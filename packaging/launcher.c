// Juniper's app launcher: runs the bundled Python inside this process, so macOS sees one app
// ("Juniper", io.github.michaelbroadbrand.juniper) using the microphone and coming to the front, rather than a
// separate Python process it won't let take focus.
//
//   Contents/MacOS/Juniper    →  Contents/Resources/python/lib/libpython3.11.dylib
//                                Contents/Resources/app/boot.py → juniper.py
#include <dlfcn.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void up(char *path, int levels) {          // strip the last `levels` path components
    while (levels-- > 0) {
        char *slash = strrchr(path, '/');
        if (slash) *slash = '\0';
    }
}

// Apps opened from Finder get a bare PATH; Claude Code's tools need the one your shell sets up.
static void login_path(void) {
    struct passwd *pw = getpwuid(getuid());
    const char *shell = (pw && pw->pw_shell && *pw->pw_shell) ? pw->pw_shell : "/bin/zsh";
    char cmd[PATH_MAX + 64];
    snprintf(cmd, sizeof cmd, "'%s' -lc 'printf %%s \"$PATH\"' 2>/dev/null", shell);
    FILE *p = popen(cmd, "r");
    char buf[16384] = "";
    if (p) {
        size_t n = fread(buf, 1, sizeof buf - 1, p);
        buf[n] = '\0';
        pclose(p);
    }
    const char *home = getenv("HOME") ? getenv("HOME") : "";
    char path[20000];
    snprintf(path, sizeof path, "%s/.local/bin:/opt/homebrew/bin:/usr/local/bin:%s",
             home, *buf ? buf : "/usr/bin:/bin:/usr/sbin:/sbin");
    setenv("PATH", path, 1);
}

int main(int argc, char **argv) {
    (void)argc;
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) return 1;

    char app[PATH_MAX], res[PATH_MAX], pyhome[PATH_MAX], lib[PATH_MAX], script[PATH_MAX], python[PATH_MAX];
    strlcpy(app, real, sizeof app);
    up(app, 3);                                              // …/Juniper.app
    snprintf(res, sizeof res, "%s/Contents/Resources", app);
    snprintf(pyhome, sizeof pyhome, "%s/python", res);
    snprintf(lib, sizeof lib, "%s/lib/libpython3.11.dylib", pyhome);
    snprintf(script, sizeof script, "%s/app/boot.py", res);   // picks the newest code update, or the app's own
    snprintf(python, sizeof python, "%s/bin/python3", pyhome);

    // Your data folder, and errors into a log there (an app has no terminal to print to).
    const char *home = getenv("HOME") ? getenv("HOME") : "/tmp";
    char data[PATH_MAX], err[PATH_MAX];
    snprintf(data, sizeof data, "%s/Library/Application Support/Juniper", home);
    mkdir(data, 0700);
    snprintf(err, sizeof err, "%s/juniper.err", data);
    freopen("/dev/null", "w", stdout);
    freopen(err, "a", stderr);

    // Once you've approved the app (Open Anyway), drop the download flag from everything inside
    // it so macOS doesn't stop its Python and helpers separately.
    char cmd[PATH_MAX + 64];
    snprintf(cmd, sizeof cmd, "/usr/bin/xattr -dr com.apple.quarantine '%s' 2>/dev/null", app);
    system(cmd);

    login_path();
    setenv("PYTHONHOME", pyhome, 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("JUNIPER_BUNDLED", "1", 1);
    setenv("JUNIPER_APP", app, 1);
    setenv("JUNIPER_EXE", real, 1);          // to restart the app after setup
    setenv("JUNIPER_PYTHON", python, 1);     // a plain Python for helpers (the permission bridge)

    void *h = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!h) { fprintf(stderr, "Juniper: can't load Python: %s\n", dlerror()); return 1; }
    int (*py_main)(int, char **) = (int (*)(int, char **))dlsym(h, "Py_BytesMain");
    if (!py_main) { fprintf(stderr, "Juniper: Python has no Py_BytesMain\n"); return 1; }
    char *args[] = {argv[0], script, NULL};
    return py_main(2, args);
}
