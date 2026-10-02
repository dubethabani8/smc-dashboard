"""
One-off: ask Deriv directly for every synthetic index's exact symbol
code, rather than guessing from naming patterns (which just got
BOOM150 wrong). Run this once, doesn't touch anything else.
"""
import asyncio

import deriv_client


async def main():
    indices = await deriv_client.fetch_synthetic_indices()
    print(f"{len(indices)} synthetic indices available:\n")
    for i in indices:
        print(f"  {i['symbol']:12s} {i['display_name']}  ({i['submarket']})")

    print("\n--- specifically looking for Boom / Step ---")
    for i in indices:
        if "boom" in i["display_name"].lower() or "step" in i["display_name"].lower():
            print(f"  {i['symbol']:12s} <- {i['display_name']}")


if __name__ == "__main__":
    asyncio.run(main())
