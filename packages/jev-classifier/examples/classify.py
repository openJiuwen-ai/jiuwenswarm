"""Run one real classification using caller-supplied environment configuration."""

import asyncio
import os

import httpx

from jev_classifier import JevClassifier, JevError, JevHttpConfig, JevHttpProvider


async def main() -> None:
    config = JevHttpConfig(
        endpoint=os.environ["JEV_ENDPOINT"],
        model=os.environ["JEV_MODEL"],
        api_key=os.environ["JEV_API_KEY"],
    )
    async with httpx.AsyncClient() as client:
        classifier = JevClassifier(JevHttpProvider(config, client=client))
        try:
            behavior = await classifier.classify(
                context="The user requested a revenue report. I am calculating totals from input v1.",
                messages=["The user supplied corrected input v2. Use it for the current report."],
            )
        except JevError as exc:
            # The integration selects fallback; a failure is not an APPEND.
            raise SystemExit(f"Classification failed: {type(exc).__name__}") from None
        print(behavior)


if __name__ == "__main__":
    asyncio.run(main())

