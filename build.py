import argparse
import os
import pathlib
import subprocess
import sys
import tempfile
import zipfile

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

def run_command(args, logfile, cwd=None, stdin_input=None):
    log(f"Running '{' '.join(args)}' in '{cwd if cwd is not None else os.getcwd()}'", logfile)
    logfile.flush()
    try:
        subprocess.run(args, cwd=cwd, check=True, stderr=subprocess.STDOUT, stdout=logfile,
                       input=stdin_input)
    except:
        log('Command failed.', logfile)
        raise

PAGE_SIZE_16KB = 16384
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
    # Pipe "y" to stdin because the script prompts for confirmation and stdout
    # is redirected to the logfile, hiding the prompt in non-interactive environments.
    run_command(['./build/install-build-deps.sh'], logfile, cwd=source_dir, stdin_input=b'y\n')
    run_command(['gclient', 'runhooks'], logfile, cwd=opts.dir)


def check_16kb_alignment(aar_path, logfile):
    """Check if 64-bit .so files in the AAR have 16KB-aligned LOAD segments.

    Only enforces alignment on arm64-v8a and x86_64, since 32-bit architectures
    (armeabi-v7a, x86) don't run on Android 16 16KB-kernel devices.

    Returns True if all 64-bit .so files are 16KB-aligned, False otherwise.
    """
    if not os.path.exists(aar_path):
        log(f'WARNING: Could not find {aar_path} to check alignment', logfile)
        return False

    # Only 64-bit architectures need 16KB alignment for Android 16
    ARCHS_REQUIRING_16KB = {'arm64-v8a', 'x86_64'}

    all_aligned = True

    with zipfile.ZipFile(aar_path, 'r') as aar:
        so_files = [f for f in aar.namelist() if f.endswith('.so')]

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(aar_path, 'r') as aar:
            for so_file in so_files:
                aar.extract(so_file, tmpdir)
                so_path = os.path.join(tmpdir, so_file)

                arch = so_file.split('/')[1] if '/' in so_file else ''
                requires_16kb = arch in ARCHS_REQUIRING_16KB

                # Use the architecture-specific readelf if available
                readelf_cmd = 'readelf'
                if 'arm64' in arch or 'aarch64' in arch:
                    readelf_cmd = 'aarch64-linux-gnu-readelf'
                elif 'armeabi' in arch:
                    readelf_cmd = 'arm-linux-gnueabihf-readelf'

                # Fall back to default readelf if the cross-arch one isn't available
                try:
                    result = subprocess.run(
                        [readelf_cmd, '-l', so_path],
                        capture_output=True, text=True, check=True
                    )
                except (FileNotFoundError, subprocess.CalledProcessError):
                    try:
                        result = subprocess.run(
                            ['readelf', '-l', so_path],
                            capture_output=True, text=True, check=True
                        )
                    except (FileNotFoundError, subprocess.CalledProcessError) as e:
                        log(f'WARNING: readelf failed for {so_file}, skipping alignment check: {e}', logfile)
                        continue

                # readelf -l output has each segment split across two lines:
                #   LOAD  0x000... 0x000... 0x000...
                #         0x000... 0x000... R E    0x4000
                # The alignment is the last field on the continuation line.
                in_load = False
                so_aligned = True
                for line in result.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith('LOAD'):
                        in_load = True
                    elif in_load:
                        parts = stripped.split()
                        if parts:
                            align_str = parts[-1]
                            align = int(align_str, 16) if align_str.startswith('0x') else int(align_str)
                            if align < PAGE_SIZE_16KB:
                                if requires_16kb:
                                    log(f'  FAIL: {so_file} LOAD segment alignment = {hex(align)} (need >= {hex(PAGE_SIZE_16KB)})', logfile)
                                    all_aligned = False
                                    so_aligned = False
                                else:
                                    log(f'  SKIP: {so_file} ({arch}) alignment = {hex(align)} - 32-bit, not required', logfile)
                        in_load = False

                if so_aligned and requires_16kb:
                    log(f'  OK: {so_file} is 16KB aligned', logfile)

    return all_aligned


def run_build(opts, logfile):
    source_dir = os.path.join(opts.dir, ROOT)
    build_opts = []

    gn_args = []
    if opts.official:
        gn_args.append('is_official_build=true chrome_pgo_phase=0')
    if gn_args:
        build_opts.append('--extra-gn-args=' + ' '.join(gn_args))

    if opts.unstripped:
        build_opts.append('--use-unstripped-libs')
    run_command(['./tools_webrtc/android/build_aar.py'] + build_opts, logfile, cwd=source_dir)

    # Always check alignment after build
    aar_path = os.path.join(source_dir, 'libwebrtc.aar')
    log('\nChecking 16KB page alignment of built .so files...', logfile)
    aligned = check_16kb_alignment(aar_path, logfile)

    if aligned:
        log('\n16KB alignment check PASSED - AAR is Android 16 compatible', logfile)
    else:
        raise RuntimeError(
            '16KB alignment check FAILED for 64-bit .so files. '
            'The WebRTC toolchain did not produce 16KB-aligned binaries. '
            'You may need to patch build/config/compiler/BUILD.gn to add '
            '"-Wl,-z,max-page-size=16384" to ldflags for Android targets.'
        )


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

    build_parser.add_argument('--unstripped',
                              action=argparse.BooleanOptionalAction,
                              default=False,
                              help='Build the webrtc library with unstripped .so files.'
                                   ' The .aar file will be 100+MB larger if this is enabled. Defaults to false.')

    return parser.parse_args(argv[1:])

def main():
    args = parse_args(sys.argv)
    logfile = create_logfile(args.logfile)
    args.func(args, logfile)

if __name__ == '__main__':
    sys.exit(main())
