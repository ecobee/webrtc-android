import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

def log(message, logfile):
    print(message)
    logfile.write((message + "\n").encode())

def create_logfile(path=None):
    if path is None:
        logfile = tempfile.NamedTemporaryFile(delete=False, prefix='build_', suffix='.log', dir='.')
    else:
        logfile = open(path, mode='w+b')
    log(f"Writing to log file '{logfile.name}'", logfile)
    return logfile

def run_command(args, logfile, cwd=None):
    log(f"Running '{' '.join(args)}' in '{cwd if cwd is not None else os.getcwd()}'", logfile)
    logfile.flush()
    try:
        subprocess.run(args, cwd=cwd, check=True, stderr=subprocess.STDOUT, stdout=logfile)
    except:
        log('Command failed.', logfile)
        raise

ROOT = "src"
GCLIENT_SPEC = """solutions = [
  {
    "name": "src",
    "url": "https://webrtc.googlesource.com/src.git",
    "deps_file": "DEPS",
    "managed": False,
    "custom_deps": {},
  },
]
target_os = ["android", "unix"]
"""

def run_fetch(opts, logfile):
    run_command(['gclient', 'root'], logfile, cwd=opts.dir)
    run_command(['gclient', 'config', '--spec', GCLIENT_SPEC], logfile, cwd=opts.dir)

    sync_opts = []
    if not opts.history:
        sync_opts.append('--no-history')
    if opts.revision is not None:
        sync_opts.extend(['--revision', f'{ROOT}@{opts.revision}'])
    run_command(['gclient', 'sync', '--delete_unversioned_trees', '--nohooks'] + sync_opts, logfile, cwd=opts.dir)

    source_dir = os.path.join(opts.dir, ROOT)
    run_command(['git', 'config', 'diff.ignoreSubmodules', 'dirty'], logfile, cwd=source_dir)
    run_command(['./build/install-build-deps.sh'], logfile, cwd=source_dir)
    run_command(['gclient', 'runhooks'], logfile, cwd=opts.dir)


def run_build(opts, logfile):
    source_dir = os.path.join(opts.dir, ROOT)
    build_opts = []
    if opts.official:
        build_opts.append('--extra-gn-args=is_official_build=true')
    run_command(['./tools_webrtc/android/build_aar.py'] + build_opts, logfile, cwd=source_dir)

def parse_args(argv):
    parser = argparse.ArgumentParser()

    parser.add_argument('--dir',
                        type=pathlib.Path,
                        default=os.getcwd(),
                        help='The directory to work in. Uses current directory by default.')
    parser.add_argument('--logfile',
                        type=pathlib.Path,
                        default=None,
                        help='The name of a logfile to use. A random one will be generated if not specified.')

    subparsers = parser.add_subparsers(required=True,
                                       title="Commands")

    fetch_parser = subparsers.add_parser('fetch',
                                         help='Configure gclient and fetch source code')
    fetch_parser.set_defaults(func=run_fetch)
    fetch_parser.add_argument('--revision',
                              type=str,
                              help='The commit or branch to check out, e.g. \'branch-heads/6045\'')
    fetch_parser.add_argument('--history',
                              action=argparse.BooleanOptionalAction,
                              default=False,
                              help='Fetch full git history (takes more time and space). Defaults to false.')

    build_parser = subparsers.add_parser('build',
                                         help='Build webrtc-android')
    build_parser.set_defaults(func=run_build)
    build_parser.add_argument('--official',
                              action=argparse.BooleanOptionalAction,
                              default=True,
                              help='Enable the "official" build level of optimization.'
                                   ' Should be true for any build shipped to end-users. Defaults to true.')

    return parser.parse_args(argv[1:])

def main():
    args = parse_args(sys.argv)
    logfile = create_logfile(args.logfile)
    args.func(args, logfile)

if __name__ == '__main__':
    sys.exit(main())
