"""Require a real wheel-only beta.2 environment before running its ACP contract suite."""
import argparse
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True, help="Python in a disposable pip/uv environment containing aelix==0.1.0b2")
    parser.add_argument("--installed-acp", type=Path, help="Test the installed entrypoint without source PYTHONPATH")
    args = parser.parse_args()
    python = args.python.absolute()
    verify = '''import importlib.metadata as m,json
names=("aelix","aelix-ai","aelix-agent-core","aelix-coding-agent")
for name in names:
 d=m.distribution(name)
 assert d.version=="0.1.0b2", (name,d.version)
 assert d.read_text("direct_url.json") is None, "Wheel-only acceptance refuses source/editable installs: "+name
print(json.dumps({name:m.version(name) for name in names}))
'''
    subprocess.run([str(python), "-I", "-c", verify], check=True)
    scripts = Path(subprocess.check_output([str(python), "-I", "-c",
        "import sysconfig; print(sysconfig.get_path('scripts'))"], text=True).strip())
    executable = scripts / ("aelix.exe" if os.name == "nt" else "aelix")
    if not executable.is_file():
        raise SystemExit("The beta.2 environment must contain its own aelix entrypoint")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, AELIX_ACP_TEST_BETA2=str(executable), PYTHONPATH=str(root / "src"))
    if args.installed_acp:
        installed = args.installed_acp.absolute()
        if not installed.is_file():
            raise SystemExit("The installed ACP entrypoint does not exist")
        env["AELIX_ACP_TEST_INSTALLED_CLI"] = str(installed)
    raise SystemExit(subprocess.call([str(python), "-m", "unittest", "discover", "-s", str(root / "tests"),
                                     "-p", "test_beta2_runtime.py", "-v"], cwd=root, env=env))


if __name__ == "__main__":
    main()
