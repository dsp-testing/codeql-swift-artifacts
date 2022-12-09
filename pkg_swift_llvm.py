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


def getoptions():
    parser = argparse.ArgumentParser(description="package swift for codeql compilation")
    parser.add_argument(f"--llvm-build-tree", required=True, type=resolve,
                        metavar="DIR", help=f"path to LLVM build tree")
    parser.add_argument(f"--swift-build-tree", required=True, type=resolve,
                        metavar="DIR", help=f"path to Swift build tree")
    parser.add_argument(f"--swift-source-tree", required=True, type=resolve,
                        metavar="DIR", help=f"path to Swift source tree")

    default_output = f"swift-prebuilt-{get_platform()}"
    parser.add_argument("--output", "-o", type=pathlib.Path, metavar="DIR_OR_ZIP",
                        help="output zip file or directory "
                             f"(by default the filename is {default_output})")

    opts = parser.parse_args()
    if opts.output is None:
        opts.output = pathlib.Path()
    opts.output = get_tgt(opts.output, default_output)

    return opts


Libs = namedtuple("Libs", ("archive", "static", "shared", "linker_flags"))

EXPORTED_LIB = "CodeQLSwiftFrontendTool"


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


def get_platform():
    return "linux" if platform.system() == "Linux" else "macos"


def configure_dummy_project(tmp, *, llvm=None, swift=None):
    print("configuring dummy cmake project")
    script_dir = pathlib.Path(os.path.realpath(__file__)).parent
    print(script_dir)
    shutil.copy(script_dir / "CMakeLists.txt", tmp / "CMakeLists.txt")
    shutil.copy(script_dir / "empty.cpp", tmp / "empty.cpp")
    tgt = tmp / "build"
    tgt.mkdir()
    run(["cmake", f"-DCMAKE_PREFIX_PATH={llvm};{swift}", "-DBUILD_SHARED_LIBS=OFF", ".."],
        cwd=tgt)
    return tgt


def get_libs(configured):
    print("extracting linking information from dummy project")
    with open(configured / "CMakeFiles" / "codeql-swift-artifacts.dir" / "link.txt") as link:
        libs = link.read().split()
        libs = libs[libs.index('codeql-swift-artifacts')+1:] # skip up to -o dummy
    ret = Libs([], [], [], [])
    for l in libs:
        if l.endswith(".a"):
            ret.static.append(str((configured / l).resolve()))
        elif l.endswith(".so") or l.endswith(".tbd") or l.endswith(".dylib"):
            l = pathlib.Path(l).stem
            ret.shared.append(f"-l{l[3:]}")  # drop 'lib' prefix and '.so' suffix
        elif l.startswith("-l"):
            ret.shared.append(l)
        elif l.startswith("-L") or l.startswith("-Wl"):
            ret.linker_flags.append(l)
        else:
            raise ValueError(f"cannot understand link.txt: " + l)
    return ret


def get_tgt(tgt, filename):
    if tgt.is_dir():
        tgt /= filename
    return tgt.resolve()


def create_static_lib(tgt, libs):
    tgt = get_tgt(tgt, f"lib{EXPORTED_LIB}.a")
    print(f"packaging {tgt.name}")
    if sys.platform == 'linux':
        includedlibs = "\n".join(f"addlib {l}" for l in libs.archive + libs.static)
        mriscript = f"create {tgt}\n{includedlibs}\nsave\nend"
        run(["ar", "-M"], cwd=tgt.parent, input=mriscript)
    else:
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
    libname = f"lib{EXPORTED_LIB}.{ext}"
    tgt = get_tgt(tgt, libname)
    print(f"packaging {libname}")
    compiler = os.environ.get("CC", "clang")
    cmd = [compiler, "-shared"]
    cmd.extend(libs.linker_flags)

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
        srcdir = src / dir
        for ext in exts:
            for srcfile in srcdir.rglob(f"*.{ext}"):
                tgtfile = tgt / dir / srcfile.relative_to(srcdir)
                tgtfile.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(srcfile, tgtfile)


def export_sdk(tgt, swift_source_tree, swift_build_tree):
    print("assembling sdk")
    srcdir = swift_build_tree/ "lib" / "swift"
    tgtdir = tgt / "usr" / "lib" / "swift"
    if get_platform() == "linux":
        srcdir /= "linux"
        tgtdir /= "linux/x86_64"
    else:
        srcdir /= "macosx"
    for mod in srcdir.glob("*.swiftmodule"):
        shutil.copytree(mod, tgtdir / mod.name)
    shutil.copytree(swift_source_tree / "stdlib" / "public" / "SwiftShims",
                    tgt / "usr" / "include" / "SwiftShims",
                    ignore=shutil.ignore_patterns('CMakeLists.txt'))


def export_libs(exported_dir, libs):
    print("exporting libraries")
    exportedlibs = [
            create_static_lib(exported_dir, libs),
            create_shared_lib(exported_dir, libs)
            ]
    for l in exportedlibs:
        l.rename(exported_dir / l.name)


def export_headers(exported_dir, swift_source_tree, llvm_build_tree, swift_build_tree):
    print("exporting headers")
    # Assuming default checkout where LLVM sources are placed next to Swift sources
    llvm_source_tree = swift_source_tree.parent / 'llvm-project/llvm'
    clang_source_tree = swift_source_tree.parent / 'llvm-project/clang'
    clang_tools_build_tree = llvm_build_tree / 'tools/clang'
    header_dirs = [ llvm_source_tree, clang_source_tree, swift_source_tree, llvm_build_tree, swift_build_tree, clang_tools_build_tree ]
    for h in header_dirs:
        copy_includes(h, exported_dir)


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
    configured = configure_dummy_project(tmp, llvm=opts.llvm_build_tree, swift=opts.swift_build_tree)
    libs = get_libs(configured)

    exported = tmp / "exported"
    exported.mkdir()
    export_libs(exported, libs)
    export_headers(exported, opts.swift_source_tree, opts.llvm_build_tree, opts.swift_build_tree)
    export_sdk(exported / "sdk", opts.swift_source_tree, opts.swift_build_tree)

    zip_dir(exported, opts.output)
    tar_dir(exported, opts.output)


if __name__ == "__main__":
    main(getoptions())

