#!/usr/bin/env python3
"""
clean_8051_ida.py  –  Strip & translate an IDA-style C51 listing
into something ASEM-51/Proteus 8 Pro will actually assemble.

Usage:
    python clean_8051_ida.py raw_ida.asm cleaned.asm
"""
import re
import sys
from pathlib import Path

# ────────────────────────────────────────────────────────── helpers
def kill_header(lines):
    """
    Throw away the banner & meta block IDA adds on top
    (everything until the first blank line).
    """
    out, skipping = [], True
    for ln in lines:
        if skipping and ln.strip() == '':
            skipping = False
            continue
        if not skipping:
            out.append(ln)
    return out

def rewrite_directives(ln: str) -> str:
    """
    • .segment code  →   CSEG
    • .segment RAM   →   DSEG   (Proteus just ignores unknown segs)
    • .segment FSR   →   ; (commented – SFR list can live in header)
    • .byte          →   DB
    • .equ           →   EQU
    """
    ln = re.sub(r'^\s*;\s*segment\s+code.*$',         'CSEG', ln, flags=re.I)
    ln = re.sub(r'^\s*;\s*segment\s+rom.*$',          'CSEG', ln, flags=re.I)
    ln = re.sub(r'^\s*;\s*segment\s+ram.*$',          'DSEG', ln, flags=re.I)
    ln = re.sub(r'^\s*;\s*segment\s+fsr.*$',          '; FSR list follows', ln, flags=re.I)
    ln = re.sub(r'\.segment\s+\w+',                   'CSEG', ln, flags=re.I)
    ln = re.sub(r'\.byte\b',                          'DB',   ln, flags=re.I)
    ln = re.sub(r'\.equ\b',                           'EQU',  ln, flags=re.I)
    return ln

# regexes for entire-line nukes
JUNK = re.compile(
    r"""^\s*;(?:
        (\s*={3,})|                    # ====== banners
        (\s*SUBROUTINE)|               # IDA sub-headers
        (\s*FUNCTION\s+CHUNK)|         # code chunks
        (\s*CODE|DATA)\s+XREF|         # cross-refs
        (\s*\[.+BYTES:)|               # collapsed-function markers
        (\s*Input\sSHA256)|            # meta untilled
        (\s*end\s+of\s+'(code|ROM|RAM)')  # IDA end markers
    )""", re.X | re.I)

def is_garbage(ln: str) -> bool:
    return bool(JUNK.match(ln))

# ────────────────────────────────────────────────────────── main
def clean(src: Path, dst: Path):
    with src.open(encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    lines = kill_header(lines)

    cleaned = []
    for ln in lines:
        if is_garbage(ln):
            continue
        ln = rewrite_directives(ln)
        cleaned.append(ln.rstrip() + '\n')

    with dst.open('w', encoding='utf-8') as f:
        f.writelines(cleaned)

    print(f"[+] Wrote cleaned file → {dst}")

if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit("Usage: python clean_8051_ida.py raw_ida.asm cleaned.asm")
    clean(Path(sys.argv[1]), Path(sys.argv[2]))
