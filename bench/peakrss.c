/* peakrss CMD ARGS...: runs the command and prints "@@<peak resident KiB>" on stderr when it ends.
   (Python's own resource.wait4 would count the parent's memory too, so the harness uses this tiny launcher.) */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/resource.h>
#include <sys/wait.h>
int main(int argc, char **argv) {
    if (argc < 2) return 2;
    pid_t pid = fork();
    if (pid == 0) { execvp(argv[1], argv + 1); _exit(127); }
    int status; struct rusage ru;
    wait4(pid, &status, 0, &ru);
    fprintf(stderr, "@@%ld\n", ru.ru_maxrss);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
