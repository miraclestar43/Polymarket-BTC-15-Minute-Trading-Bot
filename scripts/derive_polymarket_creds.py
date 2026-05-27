#!/usr/bin/env python3
"""
Derive Polymarket CLOB L2 Credentials

Uses your local private key to derive API credentials for the
Polymarket CLOB. Does NOT place orders, approve contracts, or
expose secrets beyond the 3 credential lines printed at the end.

Usage:
    python scripts/derive_polymarket_creds.py
"""
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

load_dotenv()


def mask_address(addr: str) -> str:
    """Mask address for safe display."""
    if not addr or len(addr) < 10:
        return "****"
    return f"{addr[:6]}…{addr[-4:]}"


def main():
    print("=" * 60)
    print("Derive Polymarket CLOB L2 Credentials")
    print("=" * 60)

    # Load private key (never print it)
    private_key = os.getenv("POLYMARKET_PK", "").strip() or os.getenv("PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: Neither POLYMARKET_PK nor PRIVATE_KEY found in .env")
        sys.exit(1)

    print(f"Private key: [loaded, not printed]")

    # CLOB host and chain
    clob_host = "https://clob.polymarket.com"
    chain_id = 137
    print(f"CLOB host: {clob_host}")
    print(f"Chain ID: {chain_id}")

    # Check for funder/signature_type
    funder = os.getenv("SAFE_ADDRESS", "").strip() or os.getenv("POLYMARKET_FUNDER", "").strip()
    sig_type_str = os.getenv("POLYMARKET_SIGNATURE_TYPE", "").strip()

    # Filter out placeholder values
    placeholder_values = {"", "your_funder_address_here", "your_safe_address_here",
                          "0x0000000000000000000000000000000000000000"}
    if funder and funder.lower() in placeholder_values:
        funder = None
    if sig_type_str and sig_type_str in ("", "0", "your_signature_type_here"):
        sig_type_str = None

    signature_type = int(sig_type_str) if sig_type_str else None

    if funder:
        print(f"Funder: {mask_address(funder)}")
    else:
        print(f"Funder: None (deriving without funder)")

    if signature_type is not None:
        print(f"Signature type: {signature_type}")
    else:
        print(f"Signature type: None (using default)")

    # Import and create client
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("ERROR: py_clob_client not installed")
        sys.exit(1)

    print("\nConnecting to Polymarket CLOB...")

    try:
        kwargs = {
            "host": clob_host,
            "chain_id": chain_id,
            "key": private_key,
        }
        if funder:
            kwargs["funder"] = funder
        if signature_type is not None:
            kwargs["signature_type"] = signature_type

        client = ClobClient(**kwargs)
        print("✓ ClobClient created")
    except Exception as e:
        error_msg = str(e)
        # Sanitize error — remove any key-like strings
        if private_key[:10] in error_msg:
            error_msg = error_msg.replace(private_key[:10], "[KEY]")
        print(f"ERROR creating ClobClient: {error_msg}")
        sys.exit(1)

    # Derive API credentials
    print("Deriving API credentials...")

    try:
        creds = client.create_or_derive_api_creds()
        print("✓ Credentials derived")
    except Exception as e:
        error_msg = str(e)
        # Sanitize error
        if private_key[:10] in error_msg:
            error_msg = error_msg.replace(private_key[:10], "[KEY]")
        print(f"\nERROR deriving credentials: {error_msg}")
        print("\nPossible causes:")
        print("  - Funder/signature_type may be required but not configured")
        print("  - Private key may not have a Polymarket L2 API key yet")
        print("  - Network connectivity issue")
        print("\nIf funder is required, set SAFE_ADDRESS or POLYMARKET_FUNDER")
        print("in .env and set POLYMARKET_SIGNATURE_TYPE (1=EOA, 2=PROXY, 3=GNOSIS_SAFE)")
        sys.exit(1)

    # Print credentials (the only secrets we output)
    print("\n" + "=" * 60)
    print("COPY THESE THREE LINES INTO YOUR .env FILE:")
    print("=" * 60)
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_PASSPHRASE={creds.api_passphrase}")
    print("=" * 60)
    print("\nDo NOT share these values. Paste them into .env manually.")


if __name__ == "__main__":
    main()
