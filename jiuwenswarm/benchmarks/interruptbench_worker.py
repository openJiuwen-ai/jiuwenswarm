"""Private stdio worker: model prompts in, browser action text out."""
import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


async def serve(args, output):
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.benchmarks.duplex_runtime import Events, load_models
    from jiuwenswarm.benchmarks.interruptbench_runner import NativeBrowserBridge

    bridge = NativeBrowserBridge(models=load_models(Path(args.models)), policy=args.policy,
        events=Events(Path(args.metrics)), official_records=[])
    await Runner.start()
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                request = json.loads(line)
                if request.get("operation") == "reset":
                    await bridge.reset_native(request.get("task"), request.get("environment"))
                    response = {"reset": True}
                else:
                    config = SimpleNamespace(model=request["model"], gen_config=request["gen_config"])
                    answer = await bridge._predict(config, request["prompt"], request["current"])
                    response = {"output": answer}
            except Exception as error:
                # Detailed SDK diagnostics go to stderr; stdout is protocol only.
                response = {"error": f"Native worker failed: {type(error).__name__}; inspect native log"}
                import traceback
                traceback.print_exc(file=sys.stderr)
            output.write(json.dumps(response) + "\n")
            output.flush()
    finally:
        await bridge.close_native()
        await Runner.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True)
    parser.add_argument("--metrics", required=True)
    from jiuwenswarm.benchmarks.interruptbench_runner import POLICIES
    parser.add_argument("--policy", choices=[p for p in POLICIES if p != "official"], required=True)
    args = parser.parse_args()
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        asyncio.run(serve(args, output))


if __name__ == "__main__":
    main()
