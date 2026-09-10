// Atomically rename one path only when the destination does not exist.
//
// Plain `mv source existing-directory` succeeds by nesting source inside the
// destination. That is unsafe for App/release activation: a destination that
// appears between the preflight check and `mv` can make a failed transaction
// look committed and cause its only rollback copy to be deleted. macOS exposes
// the required no-replace primitive directly as renamex_np(RENAME_EXCL).

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stdio.h>

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: rename-no-replace SOURCE DESTINATION\n");
        return 2;
    }

    if (renamex_np(argv[1], argv[2], RENAME_EXCL) == 0) {
        return 0;
    }

    int error = errno;
    fprintf(
        stderr,
        "rename-no-replace: %s -> %s: %s\n",
        argv[1],
        argv[2],
        strerror(error)
    );
    return 1;
}
