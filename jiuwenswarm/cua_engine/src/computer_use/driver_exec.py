"""Exec-only FD adapter for the existing native driver's 3/4 pipe contract.

Using exec avoids fork-time Python callbacks in a possibly multithreaded host.
"""
import os
import sys


def main():
    preview, lifetime = int(sys.argv[1]), int(sys.argv[2])
    command = sys.argv[3:]
    descriptors = [(target, os.dup(source)) for target, source in ((3, preview), (4, lifetime)) if source >= 0]
    for target, source in descriptors:
        os.dup2(source, target, inheritable=True)
        # dup2(fd, fd) is a no-op on macOS; clear CLOEXEC explicitly there too.
        os.set_inheritable(target, True)
    for _, source in descriptors:
        if source not in (3, 4):
            os.close(source)
    for source in (preview, lifetime):
        if source >= 0 and source not in (3, 4):
            os.close(source)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
