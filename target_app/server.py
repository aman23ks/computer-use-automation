"""Mock legacy core-banking teller console ("Meridian CoreOne Teller 4.2").

Intentionally hostile to automation: framesets, table layout, no ids, no <label>,
no test ids, opaque form field names, JS-only links, native confirm() dialogs.
Runtime faults can be injected through /__faults (a test hook the agent's policy blocks).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from aiohttp import web

from target_app.data import Account, Member, seed_members

VARIANTS: dict[str, dict[str, str]] = {
    "base": {
        "brand": "Meridian Community Credit Union",
        "member_label": "Member Number:",
        "search_button": "Search",
        "balance_header": "Current Balance",
        "inquiry_title": "Member Inquiry",
    },
    "lakeshore": {
        "brand": "Lakeshore Federal Credit Union",
        "member_label": "Account Number:",
        "search_button": "Find",
        "balance_header": "Ledger Balance",
        "inquiry_title": "Member Inquiry",
    },
}

FAULT_TYPES = {"interstitial", "session_expire", "app_error", "alert_dialog", "pwd_notice"}
SESSION_COOKIE = "CORESESSID"


@dataclass
class Session:
    user: str
    expires_at: float
    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class FaultState:
    pending: list[dict[str, Any]] = field(default_factory=list)
    slow_ms: int = 0

    def take(self, kind: str) -> bool:
        for fault in self.pending:
            if fault["type"] == kind and fault["remaining"] > 0:
                fault["remaining"] -= 1
                return True
        return False


@dataclass
class AppState:
    variant: dict[str, str]
    username: str
    password: str
    session_ttl_s: int
    members: dict[str, Member] = field(default_factory=seed_members)
    sessions: dict[str, Session] = field(default_factory=dict)
    faults: FaultState = field(default_factory=FaultState)
    confirmations: list[dict[str, str]] = field(default_factory=list)


STATE: web.AppKey[AppState] = web.AppKey("state", AppState)
SESSION: web.RequestKey[Session] = web.RequestKey("session", Session)

e = html.escape


def money(value: Decimal) -> str:
    return f"(${-value:,.2f})" if value < 0 else f"${value:,.2f}"


def doc(title: str, body: str, *, onload: str = "") -> str:
    return f"""<html><head><title>{e(title)}</title>
<meta http-equiv="Pragma" content="no-cache">
<script>function go(m){{document.forms['nav'].mid.value=m;document.forms['nav'].submit();}}</script>
</head><body bgcolor="#E8E8E0" leftmargin="4" topmargin="4"{f' onload="{onload}"' if onload else ''}>
<font face="Verdana,Arial" size="2">{body}</font>
<form name="nav" method="GET" action="/app/member.do"><input type="hidden" name="mid"></form>
</body></html>"""


def title_bar(text: str) -> str:
    return (
        '<table width="100%" cellpadding="3" cellspacing="0" bgcolor="#003366"><tr><td>'
        f'<font color="#FFFFFF" size="4"><b>{e(text)}</b></font></td>'
        f'<td align="right"><font color="#CCCCCC" size="1">{time.strftime("%m/%d/%Y")}</font></td></tr></table><br>'
    )


def error_line(text: str) -> str:
    return f'<table><tr><td><img src="data:," width="1" height="1"><font color="#CC0000"><b>{e(text)}</b></font></td></tr></table>'


def html_response(body: str, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/html")


def current_session(request: web.Request) -> Session | None:
    state = request.app[STATE]
    token = request.cookies.get(SESSION_COOKIE)
    if not token or token not in state.sessions:
        return None
    sess = state.sessions[token]
    if sess.expires_at < time.time():
        del state.sessions[token]
        return None
    sess.expires_at = time.time() + state.session_ttl_s
    return sess


def login_page(state: AppState, message: str = "") -> str:
    msg = error_line(message) if message else ""
    body = f"""{title_bar("CoreOne Teller - Sign On")}{msg}
<form method="POST" action="/app/login">
<table border="0" cellpadding="4" cellspacing="0" bgcolor="#F4F4EC">
<tr><td colspan="2"><font size="2">{e(state.variant["brand"])} - authorized personnel only</font></td></tr>
<tr><td align="right"><font size="2">Operator ID:</font></td><td><input type="text" name="f1" size="12" maxlength="12"></td></tr>
<tr><td align="right"><font size="2">Password:</font></td><td><input type="password" name="f2" size="12"></td></tr>
<tr><td></td><td><input type="submit" value="Sign On"></td></tr>
</table></form>"""
    return doc("CoreOne Teller", body)


@web.middleware
async def faults_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    state = request.app[STATE]
    path = request.path
    if state.faults.slow_ms and path.startswith("/app/"):
        await asyncio.sleep(state.faults.slow_ms / 1000)
    if not path.startswith("/app/") or path.startswith(("/app/login", "/app/notice")):
        return await handler(request)  # type: ignore[no-any-return]

    sess = current_session(request)
    if sess is None:
        return html_response(login_page(state, "Please sign on."))
    if state.faults.take("session_expire"):
        state.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        return html_response(login_page(state, "Your session has expired. Please sign on again."))
    if request.method == "GET" and state.faults.take("interstitial"):
        return html_response(notice_page("System Notice", "Scheduled maintenance tonight 11:00 PM - 1:00 AM ET. Share drafts in progress will be preserved.", "Acknowledge", str(request.rel_url)))
    if request.method == "GET" and state.faults.take("pwd_notice"):
        return html_response(notice_page("Password Expiration Warning", "Your operator password expires in 3 days. Contact the security office to rotate it.", "Remind Me Later", str(request.rel_url)))
    request[SESSION] = sess
    return await handler(request)  # type: ignore[no-any-return]


def notice_page(title: str, text: str, button: str, next_url: str) -> str:
    body = f"""{title_bar(title)}
<table width="70%" border="1" cellpadding="8" cellspacing="0" bgcolor="#FFFFE0"><tr><td>
<font size="2">{e(text)}</font></td></tr></table><br>
<form method="POST" action="/app/notice/ack"><input type="hidden" name="n" value="{e(next_url)}">
<input type="submit" value="{e(button)}"></form>"""
    return doc(title, body)


async def notice_ack(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    target = str(form.get("n", "/app/home"))
    if not target.startswith("/app/"):
        target = "/app/home"
    raise web.HTTPFound(target)


async def index(request: web.Request) -> web.StreamResponse:
    return html_response("""<html><head><title>CoreOne Teller</title></head>
<frameset rows="48,*" border="0" frameborder="0">
  <frame name="banner" src="/banner" scrolling="no" noresize>
  <frameset cols="170,*" border="1">
    <frame name="nav" src="/nav">
    <frame name="main" src="/app/home">
  </frameset>
</frameset></html>""")


async def banner(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    return html_response(
        f'<html><body bgcolor="#001A33" topmargin="6"><font face="Arial" color="#FFFFFF" size="3"><b>'
        f"{e(state.variant['brand'])}</b></font> <font color=\"#99AACC\" size=\"1\">CoreOne Teller 4.2.7</font></body></html>"
    )


async def nav(request: web.Request) -> web.StreamResponse:
    links = [("Main Menu", "/app/home"), ("Member Inquiry", "/app/inquiry.do"), ("Sign Off", "/app/logout")]
    rows = "".join(
        f'<tr><td><font size="2"><a href="{href}" target="main">{e(label)}</a></font></td></tr>' for label, href in links
    )
    return html_response(f'<html><body bgcolor="#D0D8E0"><table cellpadding="3">{rows}</table></body></html>')


async def login_get(request: web.Request) -> web.StreamResponse:
    return html_response(login_page(request.app[STATE]))


async def login_post(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    form = await request.post()
    if form.get("f1") != state.username or form.get("f2") != state.password:
        return html_response(login_page(state, "SIGN-ON FAILED: invalid operator ID or password."))
    token = secrets.token_hex(16)
    state.sessions[token] = Session(user=state.username, expires_at=time.time() + state.session_ttl_s)
    resp = web.HTTPFound("/app/home")
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Lax")
    raise resp


async def logout(request: web.Request) -> web.StreamResponse:
    request.app[STATE].sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    return html_response(login_page(request.app[STATE], "You have signed off."))


async def home(request: web.Request) -> web.StreamResponse:
    body = f"""{title_bar("Teller Main Menu")}
<table border="0" cellpadding="6"><tr>
<td><a href="/app/inquiry.do"><font size="3">Member Inquiry</font></a></td>
<td><font color="#888888" size="3">Cash Drawer (unavailable)</font></td>
</tr></table>"""
    return html_response(doc("Teller Main Menu", body))


async def inquiry(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    v = state.variant
    q = request.query.get("q1", "")
    result = ""
    if request.query.get("act") == "S":
        result = search_result(state, q.strip())
    body = f"""{title_bar(v["inquiry_title"])}
<form method="GET" action="/app/inquiry.do"><input type="hidden" name="act" value="S">
<table border="0" cellpadding="3" cellspacing="0">
<tr><td><font size="2">{e(v["member_label"])}</font></td>
<td><input type="text" name="q1" size="10" maxlength="10" value="{e(q)}"></td>
<td><input type="submit" value="{e(v["search_button"])}"></td></tr></table></form>
{result}"""
    return html_response(doc(v["inquiry_title"], body))


def search_result(state: AppState, q: str) -> str:
    if not (q.isdigit() and len(q) == 6):
        return error_line("ERR-104: Member number must be 6 digits.")
    member = state.members.get(q)
    if member is None:
        return error_line("No member record found for the specified number.")
    if member.restricted:
        return error_line("ACCESS DENIED - Restricted account. Supervisor override required. (SEC-403)")
    return f"""<table border="1" cellpadding="3" cellspacing="0" bgcolor="#FFFFFF">
<tr bgcolor="#C0C8D0"><td><b>Member #</b></td><td><b>Name</b></td><td><b>Branch</b></td><td><b>&nbsp;</b></td></tr>
<tr><td>{e(member.number)}</td><td>{e(member.name)}</td><td>{e(member.branch)}</td>
<td><a href="javascript:void(0)" onclick="go('{e(member.internal_id)}')">Select</a></td></tr></table>"""


def member_by_internal(state: AppState, mid: str) -> Member | None:
    return next((m for m in state.members.values() if m.internal_id == mid), None)


async def member_detail(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    member = member_by_internal(state, request.query.get("mid", ""))
    if state.faults.take("app_error"):
        return html_response(
            doc("Error", title_bar("Application Error") + "<pre>HTTP Status 500 - java.lang.NullPointerException\n"
                "\tat com.meridian.core.teller.MemberAction.execute(MemberAction.java:212)</pre>"),
            status=500,
        )
    if member is None:
        return html_response(doc("Member", title_bar("Member Detail") + error_line("No member record found for the specified number.")))
    onload = "alert('Member has 2 pending alerts. Review in Alerts tab.')" if state.faults.take("alert_dialog") else ""
    info_rows = [("Name", member.name), ("Member #", member.number), ("SSN", member.ssn), ("Date of Birth", member.dob),
                 ("Phone", member.phone), ("Email", member.email), ("Branch", member.branch)]
    info = "".join(f'<tr><td align="right"><font size="2">{e(k)}:</font></td><td><font size="2"><b>{e(val)}</b></font></td></tr>' for k, val in info_rows)
    accounts = "".join(account_row(a) for a in member.accounts)
    body = f"""{title_bar("Member Detail")}
<table border="0" cellpadding="2">{info}</table><br>
<font size="3"><b>Account Summary</b></font>
<table border="1" cellpadding="3" cellspacing="0" bgcolor="#FFFFFF" width="90%">
<tr bgcolor="#003366"><td><font color="#FFFFFF"><b>Suffix</b></font></td><td><font color="#FFFFFF"><b>Description</b></font></td>
<td><font color="#FFFFFF"><b>Type</b></font></td><td><font color="#FFFFFF"><b>{e(state.variant["balance_header"])}</b></font></td>
<td><font color="#FFFFFF"><b>Available</b></font></td></tr>
{accounts}</table><br>
<table><tr>
<td><input type="button" value="Open Share" onclick="location.href='/app/share/new?mid={e(member.internal_id)}'"></td>
<td><input type="button" value="Back" onclick="location.href='/app/inquiry.do'"></td></tr></table>"""
    return html_response(doc("Member Detail", body, onload=onload))


def account_row(a: Account) -> str:
    return (f"<tr><td>{e(a.suffix)}</td><td>{e(a.description)}</td><td>{e(a.kind)}</td>"
            f'<td align="right">{money(a.balance)}</td><td align="right">{money(a.available)}</td></tr>')


SHARE_TYPES = {"RS": "Regular Savings", "MM": "Money Market", "HC": "Holiday Club"}


def share_form(member: Member, message: str = "", values: dict[str, str] | None = None) -> str:
    values = values or {}
    options = "".join(
        f'<option value="{k}"{" selected" if values.get("t") == k else ""}>{e(v)}</option>' for k, v in SHARE_TYPES.items()
    )
    body = f"""{title_bar("New Share Account")}{error_line(message) if message else ""}
<font size="2">Member: <b>{e(member.name)}</b></font><br><br>
<form method="POST" action="/app/share/new"><input type="hidden" name="mid" value="{e(member.internal_id)}">
<table border="0" cellpadding="4" cellspacing="0" bgcolor="#F4F4EC">
<tr><td align="right"><font size="2">Share Type:</font></td><td><select name="t"><option value="">-- select --</option>{options}</select></td></tr>
<tr><td align="right"><font size="2">Nickname:</font></td><td><input type="text" name="n1" size="20" maxlength="20" value="{e(values.get("n1", ""))}"></td></tr>
<tr><td align="right"><font size="2">Opening Deposit:</font></td><td><input type="text" name="d1" size="10" value="{e(values.get("d1", ""))}"></td></tr>
<tr><td></td><td><input type="submit" value="Continue"></td></tr></table></form>"""
    return doc("New Share Account", body)


async def share_new_get(request: web.Request) -> web.StreamResponse:
    member = member_by_internal(request.app[STATE], request.query.get("mid", ""))
    if member is None:
        return html_response(doc("Error", error_line("No member record found for the specified number.")))
    return html_response(share_form(member))


async def share_new_post(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    form = await request.post()
    values = {k: str(form.get(k, "")) for k in ("mid", "t", "n1", "d1")}
    member = member_by_internal(state, values["mid"])
    if member is None:
        return html_response(doc("Error", error_line("No member record found for the specified number.")))
    if values["t"] not in SHARE_TYPES:
        return html_response(share_form(member, "ERR-220: Share type is required.", values))
    try:
        deposit = Decimal(values["d1"].replace("$", "").replace(",", ""))
    except InvalidOperation:
        deposit = Decimal("-1")
    if deposit < Decimal("5.00"):
        return html_response(share_form(member, "ERR-221: Opening deposit must be at least $5.00.", values))
    draft_id = secrets.token_hex(4)
    sess = request[SESSION]
    sess.drafts[draft_id] = {"member": member.number, "type": values["t"], "nickname": values["n1"], "deposit": deposit}
    body = f"""{title_bar("Review New Share")}
<table border="1" cellpadding="4" cellspacing="0" bgcolor="#FFFFFF">
<tr><td>Member</td><td>{e(member.name)}</td></tr>
<tr><td>Share Type</td><td>{e(SHARE_TYPES[values["t"]])}</td></tr>
<tr><td>Nickname</td><td>{e(values["n1"])}</td></tr>
<tr><td>Opening Deposit</td><td>{money(deposit)}</td></tr></table><br>
<font size="2">Submitting will open the share and post the opening deposit.</font><br><br>
<form method="POST" action="/app/share/submit" onsubmit="return confirm('Post this transaction? This cannot be undone.')">
<input type="hidden" name="dr" value="{draft_id}">
<table><tr><td><input type="submit" value="Submit"></td>
<td><input type="button" value="Edit" onclick="history.back()"></td></tr></table></form>"""
    return html_response(doc("Review New Share", body))


async def share_submit(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    form = await request.post()
    sess = request[SESSION]
    draft = sess.drafts.pop(str(form.get("dr", "")), None)
    if draft is None:
        return html_response(doc("Error", title_bar("Review New Share") + error_line("ERR-230: Draft expired or already posted.")))
    member = state.members[draft["member"]]
    suffix = f"S{11 + sum(1 for a in member.accounts if a.suffix.startswith('S1'))}"
    member.accounts.append(Account(suffix, draft["nickname"] or SHARE_TYPES[draft["type"]], "SAVINGS", draft["deposit"], draft["deposit"]))
    conf = "CU-" + secrets.token_hex(3).upper()
    state.confirmations.append({"confirmation": conf, "member": member.number, "suffix": suffix})
    body = f"""{title_bar("Transaction Complete")}
<table border="1" cellpadding="4" cellspacing="0" bgcolor="#FFFFFF">
<tr><td>Confirmation Number</td><td><b>{conf}</b></td></tr>
<tr><td>New Share Suffix</td><td>{suffix}</td></tr>
<tr><td>Opening Deposit</td><td>{money(draft["deposit"])}</td></tr></table>"""
    return html_response(doc("Transaction Complete", body))


async def faults_get(request: web.Request) -> web.StreamResponse:
    f = request.app[STATE].faults
    return web.json_response({"pending": f.pending, "slow_ms": f.slow_ms})


async def faults_post(request: web.Request) -> web.StreamResponse:
    payload = await request.json()
    f = request.app[STATE].faults
    for item in payload.get("faults", []):
        if item["type"] not in FAULT_TYPES:
            raise web.HTTPBadRequest(text=f"unknown fault {item['type']}")
        f.pending.append({"type": item["type"], "remaining": int(item.get("count", 1))})
    if "slow_ms" in payload:
        f.slow_ms = int(payload["slow_ms"])
    return web.json_response({"pending": f.pending, "slow_ms": f.slow_ms})


async def faults_delete(request: web.Request) -> web.StreamResponse:
    request.app[STATE].faults = FaultState()
    return web.json_response({"pending": [], "slow_ms": 0})


def create_app(variant: str = "base", session_ttl_s: int = 900) -> web.Application:
    app = web.Application(middlewares=[faults_middleware])
    app[STATE] = AppState(
        variant=VARIANTS[variant],
        username=os.environ.get("TARGET_OPERATOR_ID", "teller01"),
        password=os.environ.get("TARGET_OPERATOR_PASSWORD", "Demo-Only-Pass-1"),
        session_ttl_s=session_ttl_s,
    )
    app.add_routes([
        web.get("/", index),
        web.get("/banner", banner),
        web.get("/nav", nav),
        web.get("/app/login", login_get),
        web.post("/app/login", login_post),
        web.get("/app/logout", logout),
        web.post("/app/notice/ack", notice_ack),
        web.get("/app/home", home),
        web.get("/app/inquiry.do", inquiry),
        web.get("/app/member.do", member_detail),
        web.get("/app/share/new", share_new_get),
        web.post("/app/share/new", share_new_post),
        web.post("/app/share/submit", share_submit),
        web.get("/__faults", faults_get),
        web.post("/__faults", faults_post),
        web.delete("/__faults", faults_delete),
    ])
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock legacy teller console")
    parser.add_argument("--port", type=int, default=8601)
    parser.add_argument("--variant", choices=sorted(VARIANTS), default="base")
    args = parser.parse_args()
    web.run_app(create_app(args.variant), host="127.0.0.1", port=args.port, print=lambda *_: None)


if __name__ == "__main__":
    main()
