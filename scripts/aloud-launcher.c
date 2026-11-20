#include <Python.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void require_ok(PyConfig *config, PyStatus status) {
    if (PyStatus_Exception(status)) {
        PyConfig_Clear(config);
        Py_ExitStatusException(status);
    }
}

int main(int argc, char **argv) {
    char path[PATH_MAX];
    char executable[PATH_MAX];
    char runtime[PATH_MAX];
    uint32_t size = sizeof(path);
    if (_NSGetExecutablePath(path, &size) != 0 || realpath(path, executable) == NULL) {
        fputs("ALOUD could not locate its application bundle.\n", stderr);
        return EXIT_FAILURE;
    }
    const char *filename = strrchr(executable, '/');
    int length = snprintf(runtime, sizeof(runtime), "%.*s/../Resources/python",
                          (int)(filename - executable), executable);
    if (length < 0 || (size_t)length >= sizeof(runtime)) {
        fputs("ALOUD's application path is too long.\n", stderr);
        return EXIT_FAILURE;
    }

    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.parse_argv = 0;
    config.write_bytecode = 0;
    require_ok(&config, PyConfig_SetBytesArgv(&config, argc, argv));
    require_ok(&config, PyConfig_SetBytesString(&config, &config.program_name, executable));
    require_ok(&config, PyConfig_SetBytesString(&config, &config.home, runtime));
    require_ok(&config, PyConfig_SetString(&config, &config.run_module, L"aloud"));
    require_ok(&config, Py_InitializeFromConfig(&config));
    PyConfig_Clear(&config);
    return Py_RunMain();
}
