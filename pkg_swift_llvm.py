#!/usr/bin/env python3

import argparse
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import zlib
from collections import namedtuple

DEPS = {"llvm": ["LLVMSupport"],
        "swift": ["swiftFrontendTool"]}


def getoptions():
    parser = argparse.ArgumentParser(description="package swift for codeql compilation")
    for p in DEPS:
        parser.add_argument(f"--{p}", required=True, type=resolve,
                            metavar="DIR", help=f"path to {p} build root")
    default_output = f"swift-prebuilt-{get_platform()}"
    parser.add_argument("--keep-tmp-dir", "-K", action="store_true",
                        help="do not clean up the temporary directory")
    parser.add_argument("--output", "-o", type=pathlib.Path, metavar="DIR_OR_ZIP",
                        help="output zip file or directory "
                             f"(by default the filename is {default_output})")
    update_help_fmt = "Only update the {} library in DIR, triggering rebuilds of required files"
    parser.add_argument("--update-shared", "-u", metavar="DIR", type=pathlib.Path,
                        help=update_help_fmt.format("shared"))
    parser.add_argument("--update-static", "-U", metavar="DIR", type=pathlib.Path,
                        help=update_help_fmt.format("static"))
    opts = parser.parse_args()
    if opts.output and (opts.update_shared or opts.update_static):
        parser.error("provide --output or one of --update-*, not both")
    if opts.output is None:
        opts.output = pathlib.Path()
    opts.output = get_tgt(opts.output, default_output)
    return opts


Libs = namedtuple("Libs", ("archive", "static", "shared"))

DEPLIST = [x for d in DEPS.values() for x in d]

CMAKELISTS_DUMMY = f"""
cmake_minimum_required(VERSION 3.12.4)

project(dummy C CXX)

find_package(LLVM REQUIRED CONFIG PATHS ${{LLVM_ROOT}}/lib/cmake/llvm NO_DEFAULT_PATH)
find_package(Clang REQUIRED CONFIG PATHS ${{LLVM_ROOT}}/lib/cmake/clang NO_DEFAULT_PATH)
find_package(Swift REQUIRED CONFIG PATHS ${{SWIFT_ROOT}}/lib/cmake/swift NO_DEFAULT_PATH)

add_executable(dummy empty.cpp)
target_link_libraries(dummy PRIVATE {" ".join(DEPLIST)})
"""

EXPORTED_LIB = "swiftAndLlvmSupport"

CMAKELISTS_EXPORTED_FMT = """
add_library({exported} INTERFACE)

if (BUILD_SHARED_LIBS)
    if (APPLE)
        set(EXT "dylib")
    else()
        set(EXT "so")
    endif()
else()
    set(EXT "a")
endif()

set (SwiftLLVMWrapperLib libswiftAndLlvmSupportReal.${{EXT}})
set (input ${{CMAKE_CURRENT_LIST_DIR}}/${{SwiftLLVMWrapperLib}})
set (output ${{CMAKE_BINARY_DIR}}/${{SwiftLLVMWrapperLib}})

add_custom_command(OUTPUT ${{output}}
        COMMAND ${{CMAKE_COMMAND}} -E copy_if_different ${{input}} ${{output}}
        DEPENDS ${{input}})
add_custom_target(copy-llvm-swift-wrapper DEPENDS ${{output}})

target_include_directories({exported} INTERFACE ${{CMAKE_CURRENT_LIST_DIR}}/include)
target_link_libraries({exported} INTERFACE
    ${{output}}
    {libs}
)
add_dependencies(swiftAndLlvmSupport copy-llvm-swift-wrapper)
"""


class TempDir:
    def __init__(self, cleanup=True):
        self.path = None
        self.cleanup = cleanup

    def __enter__(self):
        self.path = pathlib.Path(tempfile.mkdtemp())
        return self.path

    def __exit__(self, *args):
        if self.cleanup:
            shutil.rmtree(self.path)


def resolve(p):
    return pathlib.Path(p).resolve()


def run(prog, *, cwd, env=None, input=None):
    print("running", " ".join(prog), f"(cwd={cwd})")
    if env is not None:
        runenv = dict(os.environ)
        runenv.update(env)
    else:
        runenv = None
    subprocess.run(prog, cwd=cwd, env=runenv, input=input, text=True)


def build(dir, targets):
    print(f"building {' '.join(targets)} in {dir}")
    cmd = ["cmake", "--build", ".", "--"]
    cmd.extend(targets)
    run(cmd, cwd=dir)


def get_platform():
    return "linux" if platform.system() == "Linux" else "macos"


def create_empty_cpp(path):
    with open(path / "empty.cpp", "w"):
        pass


def install(tmp, opts):
    print("installing dependencies")
    tgt = tmp / "install"
    for p in DEPS:
        builddir = getattr(opts, p)
        run(["cmake", "--build", ".", "--", "install"], cwd=builddir, env={"DESTDIR": tgt})
    if sys.platform != 'linux':
        return tgt / "Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain"
    return tgt


def configure_dummy_project(tmp, *, llvm=None, swift=None, installed=None):
    print("configuring dummy cmake project")
    if installed is not None:
        swift = llvm = installed / "usr"
    with open(tmp / "CMakeLists.txt", "w") as out:
        out.write(CMAKELISTS_DUMMY)
    create_empty_cpp(tmp)
    tgt = tmp / "build"
    tgt.mkdir()
    run(["cmake", f"-DLLVM_ROOT={llvm}", f"-DSWIFT_ROOT={swift}", "-DBUILD_SHARED_LIBS=OFF", ".."],
        cwd=tgt)
    return tgt


def get_libs(configured):
    print("extracting linking information from dummy project")
    with open(configured / "CMakeFiles" / "dummy.dir" / "link.txt") as link:
        libs = link.read().split()
        libs = libs[libs.index('dummy')+1:] # skip up to -o dummy
    ret = Libs([], [], [])
    for l in libs:
        if l.endswith(".a"):
            ret.static.append(str((configured / l).resolve()))
        elif l.endswith(".so") or l.endswith(".tbd") or l.endswith(".dylib"):
            l = pathlib.Path(l).stem
            ret.shared.append(f"-l{l[3:]}")  # drop 'lib' prefix and '.so' suffix
        elif l.startswith("-l"):
            ret.shared.append(l)
        else:
            raise ValueError(f"cannot understand link.txt: " + l)
    # move direct dependencies into archive
    ret.archive[:] = ret.static[:len(DEPLIST)]
    ret.static[:len(DEPLIST)] = []
    return ret


def get_tgt(tgt, filename):
    if tgt.is_dir():
        tgt /= filename
    return tgt.resolve()


def create_static_lib(tgt, libs):
    tgt = get_tgt(tgt, f"lib{EXPORTED_LIB}Real.a")
    print(f"packaging {tgt.name}")
    if sys.platform == 'linux':
        includedlibs = "\n".join(f"addlib {l}" for l in libs.archive + libs.static)
        mriscript = f"create {tgt}\n{includedlibs}\nsave\nend"
        run(["ar", "-M"], cwd=tgt.parent, input=mriscript)
    else:
        includedlibs = " ".join(f"{l}" for l in libs.archive + libs.static)
        libtool_args = ["libtool", "-static"]
        libtool_args.extend(libs.archive)
        libtool_args.extend(libs.static)
        libtool_args.append("-o")
        libtool_args.append(str(tgt))
        run(libtool_args, cwd=tgt.parent)
    return tgt


def create_shared_lib(tgt, libs):
    ext = "so"
    if sys.platform != 'linux':
        ext = "dylib"
    libname = f"lib{EXPORTED_LIB}Real.{ext}"
    tgt = get_tgt(tgt, libname)
    print(f"packaging {libname}")
    compiler = os.environ.get("CC", "clang")
    cmd = [compiler, "-shared"]

    if sys.platform == 'linux':
        cmd.append("-Wl,--whole-archive")
    else:
        cmd.append("-Wl,-all_load")

    cmd.append(f"-o{tgt}")
    cmd.extend(libs.archive)

    if sys.platform == 'linux':
        cmd.append("-Wl,--no-whole-archive")
    else:
        cmd.append("-lc++")

    cmd.extend(libs.static)
    cmd.extend(libs.shared)
    run(cmd, cwd=tgt.parent)
    if sys.platform != "linux":
        run(["install_name_tool", "-id", f"@executable_path/{libname}", libname], cwd=tgt.parent)
    return tgt


def copy_includes(src, tgt):
    print("copying includes")
    for dir, exts in (("include", ("h", "def", "inc")), ("stdlib", ("h",))):
        srcdir = src / "usr" / dir
        for ext in exts:
            for srcfile in srcdir.rglob(f"*.{ext}"):
                tgtfile = tgt / dir / srcfile.relative_to(srcdir)
                tgtfile.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(srcfile, tgtfile)


def create_sdk(installed, tgt):
    print("assembling sdk")
    srcdir = installed / "usr" / "lib" / "swift"
    tgtdir = tgt / "usr" / "lib" / "swift"
    if get_platform() == "linux":
        srcdir /= "linux"
        tgtdir /= "linux/x86_64"
    else:
        srcdir /= "macosx"
    for mod in srcdir.glob("*.swiftmodule"):
        shutil.copytree(mod, tgtdir / mod.name)
    shutil.copytree(installed / "usr" / "stdlib" / "public" / "SwiftShims",
                    tgt / "usr" / "include" / "SwiftShims")


def create_export_dir(tmp, installed, libs):
    print("assembling prebuilt directory")
    exportedlibs = [create_static_lib(tmp, libs), create_shared_lib(tmp, libs)]
    tgt = tmp / "exported"
    tgt.mkdir()
    for l in exportedlibs:
        l.rename(tgt / l.name)
    with open(tgt / "swift_llvm_prebuilt.cmake", "w") as out:
        # drop -l prefix here
        sharedlibs = " ".join(l[2:] for l in libs.shared)
        out.write(CMAKELISTS_EXPORTED_FMT.format(exported=EXPORTED_LIB, libs=sharedlibs))
    copy_includes(installed, tgt)
    create_sdk(installed, tgt / "sdk")
    return tgt


def zip_dir(src, tgt):
    tgt = get_tgt(tgt, f"swift-prebuilt-{get_platform()}.zip")
    print(f"compressing {src.name} to {tgt}")
    archive = shutil.make_archive(tgt, 'zip', src)
    print(f"created {archive}")

def tar_dir(src, tgt):
    tgt = get_tgt(tgt, f"swift-prebuilt-{get_platform()}.tar.gz")
    print(f"compressing {src.name} to {tgt}")
    archive = shutil.make_archive(tgt, 'gztar', src)
    print(f"created {archive}")


def main(opts):
    tmp = pathlib.Path('/tmp/llvm-swift')
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.mkdir(tmp)
    if opts.update_shared or opts.update_static:
        for project, deps in DEPS.items():
            build(getattr(opts, project), deps)
        configured = configure_dummy_project(tmp, llvm=opts.llvm, swift=opts.swift)
        libs = get_libs(configured)
        if opts.update_shared:
            create_shared_lib(opts.update_shared, libs)
        if opts.update_static:
            create_static_lib(opts.update_static, libs)
    else:
        installed = install(tmp, opts)
        swift_syntax_build = opts.swift / "include/swift/Syntax/"
        swift_syntax_install = installed / "usr/include/swift/Syntax/"
        for header in os.listdir(swift_syntax_build):
            if header.endswith('.h') or header.endswith('.def'):
                shutil.copy(swift_syntax_build / header, swift_syntax_install / header)
        configured = configure_dummy_project(tmp, installed=installed)
        libs = get_libs(configured)
        exported = create_export_dir(tmp, installed, libs)
        zip_dir(exported, opts.output)
        tar_dir(exported, opts.output)


if __name__ == "__main__":
    main(getoptions())
