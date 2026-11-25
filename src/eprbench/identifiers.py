"""NHS number validation and synthetic generation.

Synthetic numbers are drawn from the 999 000 0000 - 999 999 9999 range, which
is reserved for test purposes and never issued to real patients.
"""

from collections.abc import Iterator


def nhs_check_digit(first9: str) -> int | None:
    """Modulus 11 check digit for the first nine digits, or None if no valid digit exists."""
    total = sum(int(d) * w for d, w in zip(first9, range(10, 1, -1)))
    check = 11 - total % 11
    if check == 11:
        return 0
    return None if check == 10 else check


def normalise_nhs_number(value: str) -> str:
    return value.replace(" ", "").replace("-", "")


def is_valid_nhs_number(value: str) -> bool:
    digits = normalise_nhs_number(value)
    if len(digits) != 10 or not digits.isdigit():
        return False
    return nhs_check_digit(digits[:9]) == int(digits[9])


def format_nhs_number(value: str) -> str:
    d = normalise_nhs_number(value)
    return f"{d[:3]} {d[3:6]} {d[6:]}"


def synthetic_nhs_numbers(start: int = 1) -> Iterator[str]:
    """Yield valid test-range NHS numbers in a fixed order."""
    for n in range(start, 1_000_000):
        first9 = f"999{n:06d}"
        check = nhs_check_digit(first9)
        if check is not None:
            yield f"{first9}{check}"
