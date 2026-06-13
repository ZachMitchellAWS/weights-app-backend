"""
Email blacklist guard. Edit the constants below to block specific addresses,
whole domains, or any address containing a forbidden substring from creating
accounts, logging in, or signing in with Apple.

IMPORTANT — entries are stored in REVERSED character order. When adding an
entry, reverse the string first:
    "baddomain.com"            ->  "moc.niamoddab"
    "spammer@gmail.com"        ->  "moc.liamg@remmaps"
    "spam"                     ->  "maps"
At check time we reverse the incoming email and compare against the stored
(reversed) values, which is mathematically equivalent. This is intentional
obfuscation so the blocked strings are not directly readable in source diffs.
It is NOT a security barrier — anyone who reads this docstring can decode
trivially.

Checks are case-insensitive and ignore surrounding whitespace. Substring
matches anywhere in the address (local part, '@', or domain).
"""

from typing import Optional

# Reversed-form domains. e.g., "throwaway.example" -> "elpmaxe.yawaworht".
BLOCKED_DOMAINS: set = {
    # add reversed domains here
    "oi.rettamydob",
    "zyx.kcehctsilkcalbmetsys" #systemblacklistcheck.xyz
}

# Reversed-form addresses. e.g., "spammer@gmail.com" -> "moc.liamg@remmaps".
BLOCKED_EMAILS: set = {
    # add reversed addresses here
    
}

# Reversed-form substrings. Any address whose reversed form contains one of
# these — anywhere in the reversed local part or reversed domain — is blocked.
# Broad by design; reversed "spam" ("maps") matches both "spam@x.com" AND
# "nospam@x.com" once both sides are reversed.
BLOCKED_SUBSTRINGS: set = {
    # add reversed substrings here
    "nocaedhcra.nayr",
    "gnirtstsilkcalbmetsys" #systemblackliststring
}


def is_email_blocked(email: Optional[str]) -> bool:
    if not email:
        return False
    normalized = email.strip().lower()
    if "@" not in normalized:
        return False
    reversed_email = normalized[::-1]
    if reversed_email in BLOCKED_EMAILS:
        return True
    domain = normalized.rsplit("@", 1)[1]
    if domain[::-1] in BLOCKED_DOMAINS:
        return True
    return any(s in reversed_email for s in BLOCKED_SUBSTRINGS)
