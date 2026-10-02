from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Account:
    suffix: str
    description: str
    kind: str
    balance: Decimal
    available: Decimal


@dataclass
class Member:
    number: str
    internal_id: str
    name: str
    ssn: str
    dob: str
    phone: str
    email: str
    branch: str
    restricted: bool = False
    accounts: list[Account] = field(default_factory=list)


def seed_members() -> dict[str, Member]:
    members = [
        Member(
            number="100234",
            internal_id="48213",
            name="SMITH, JANE A",
            ssn="900-12-3456",
            dob="04/12/1981",
            phone="(555) 010-2233",
            email="jane.smith@example.test",
            branch="001 MAIN",
            accounts=[
                Account("S00", "Primary Share", "SAVINGS", Decimal("1234.56"), Decimal("1209.56")),
                Account("S10", "Holiday Club", "SAVINGS", Decimal("310.00"), Decimal("310.00")),
                Account("D20", "Free Checking", "CHECKING", Decimal("2045.18"), Decimal("1990.00")),
            ],
        ),
        Member(
            number="100777",
            internal_id="51907",
            name="NGUYEN, ROBERT K",
            ssn="900-44-1180",
            dob="11/30/1974",
            phone="(555) 010-9981",
            email="r.nguyen@example.test",
            branch="004 WESTGATE",
            accounts=[
                Account("S00", "Primary Share", "SAVINGS", Decimal("88410.02"), Decimal("88410.02")),
                Account("L40", "Auto Loan", "LOAN", Decimal("-12650.40"), Decimal("0.00")),
            ],
        ),
        Member(
            number="100999",
            internal_id="50001",
            name="EMPLOYEE, RESTRICTED",
            ssn="900-99-0001",
            dob="01/01/1990",
            phone="(555) 010-0000",
            email="restricted@example.test",
            branch="001 MAIN",
            restricted=True,
            accounts=[Account("S00", "Primary Share", "SAVINGS", Decimal("5.00"), Decimal("5.00"))],
        ),
    ]
    return {m.number: m for m in members}
