"""TEST ONLY: native POSIX lock/rename primitives, not a whole auth race test.

One subprocess follows the native lock→read→write sequence and keeps its old
descriptor locked after rename. Another invokes the ordinary public setter.
This exposes the inode boundary without editing or monkeypatching native code.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

from aelix_ai.oauth.auth_storage import AuthStorage


async def main():
    path = Path(sys.argv[2])
    store = AuthStorage(path)
    if sys.argv[1] == "holder":
        fd = store._cross_process_lock_fd()
        if fd is None:
            raise RuntimeError("This probe needs the native POSIX lock")
        try:
            current = store._read_raw()
            current["first"] = {"type": "api_key", "key": "synthetic-first"}
            store._write_raw(current)
            print(json.dumps({"heldDescriptorInodeChanged": os.fstat(fd).st_ino != path.stat().st_ino}), flush=True)
            sys.stdin.readline()
        finally:
            store._release_cross_process_lock(fd)
    else:
        await store.set_api_key("second", "synthetic-second")
        print(json.dumps({"publicSetterCompleted": True}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
