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
    parser.add_argument(f"--build-tree", required=True, type=resolve,
                        metavar="DIR", help=f"path to the build tree")
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


EXPORTED_LIB = "CodeQLSwiftFrontendTool"

Libs = namedtuple("Libs", ("static", "shared", "linker_flags"))


def resolve(p):
    return pathlib.Path(p).resolve()


def run(prog, *, cwd, env=None, input=None):
    print("running", *prog, f"(cwd={cwd})")
    if env is not None:
        runenv = dict(os.environ)
        runenv.update(env)
    else:
        runenv = None
    subprocess.run(prog, cwd=cwd, env=runenv, input=input, text=True, check=True)


def get_platform():
    return "linux" if platform.system() == "Linux" else "macos"


def configure_dummy_project(tmp, prefixes):
    print("configuring dummy cmake project")
    script_dir = pathlib.Path(os.path.realpath(__file__)).parent
    print(script_dir)
    shutil.copy(script_dir / "CMakeLists.txt", tmp / "CMakeLists.txt")
    shutil.copy(script_dir / "empty.cpp", tmp / "empty.cpp")
    tgt = tmp / "build"
    tgt.mkdir()
    prefixes = ';'.join(str(p) for p in prefixes)
    run(["cmake", f"-DCMAKE_PREFIX_PATH={prefixes}", "-DBUILD_SHARED_LIBS=OFF", ".."], cwd=tgt)
    return tgt


def get_libs(configured):
    print("extracting linking information from dummy project")
    with open(configured / "CMakeFiles" / "codeql-swift-artifacts.dir" / "link.txt") as link:
        libs = link.read().split()
        libs = libs[libs.index('codeql-swift-artifacts') + 1:]  # skip up to -o dummy
    ret = Libs([], [], [])
    for l in libs:
        if l.endswith(".a"):
            ret.static.append((configured / l).absolute())
        elif l.endswith(".so") or l.endswith(".tbd") or l.endswith(".dylib"):
            ret.shared.append((configured / l).absolute())
        elif l.startswith(("-L", "-Wl", "-l")):
            ret.linker_flags.append(l)
        else:
            raise ValueError(f"cannot understand link.txt: " + l)
    return ret


def get_tgt(tgt, filename):
    if tgt.is_dir():
        tgt /= filename
    return tgt.absolute()


def create_static_lib(tgt, libs):
    tgt = get_tgt(tgt, f"lib{EXPORTED_LIB}.a")
    print(f"packaging {tgt.name}")
    if sys.platform == 'linux':
        includedlibs = "\n".join(f"addlib {l}" for l in libs.static)
        mriscript = f"create {tgt}\n{includedlibs}\nsave\nend"
        run(["ar", "-M"], cwd=tgt.parent, input=mriscript)
    else:
        libtool_args = ["libtool", "-static"]
        libtool_args.extend(libs.static)
        libtool_args.append("-o")
        libtool_args.append(str(tgt))
        run(libtool_args, cwd=tgt.parent)
    return tgt


def copy_includes(src, tgt):
    print(f"copying includes from {src}")
    for dir, exts in (("include", ("h", "def", "inc")), ("stdlib", ("h",))):
        srcdir = src / dir
        for ext in exts:
            for srcfile in srcdir.rglob(f"*.{ext}"):
                tgtfile = tgt / dir / srcfile.relative_to(srcdir)
                tgtfile.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(srcfile, tgtfile)


def export_sdk(tgt, swift_source_tree, swift_build_tree):
    print("assembling sdk")
    srcdir = swift_build_tree / "lib" / "swift"
    tgtdir = tgt / "usr" / "lib" / "swift"
    if get_platform() == "linux":
        srcdir /= "linux"
        tgtdir /= "linux/x86_64"
    else:
        srcdir /= "macosx"
    for mod in srcdir.glob("*.swiftmodule"):
        shutil.copytree(mod, tgtdir / mod.name)
    shutil.copytree(swift_source_tree / "stdlib" / "public" / "SwiftShims" / "swift" / "shims",
                    tgt / "usr" / "lib" / "swift" / "shims",
                    ignore=shutil.ignore_patterns('CMakeLists.txt'))


def export_stdlibs(exported_dir, swift_build_tree):
    ext = 'dylib'
    platform = 'linux' if get_platform() == 'linux' else 'macosx'
    lib_dir = swift_build_tree / 'lib/swift' / platform
    patterns = [f'lib{dep}.*' for dep in (
        "dispatch",
        "swiftCore",
        "swift_*",
        "swiftGlibc",
        "swiftCompatibility*",
    )]
    for pattern in patterns:
        for stdlib in lib_dir.glob(pattern):
            print(f'Copying {stdlib}')
            shutil.copy(stdlib, exported_dir)


def export_libs(exported_dir, libs, swift_build_tree):
    print("exporting libraries")
    create_static_lib(exported_dir, libs)
    for lib in libs.shared:
        # export libraries under the build tree (e.g. libSwiftSyntax.so)
        if lib.is_relative_to(swift_build_tree.parent):
            shutil.copy(lib, exported_dir)
    export_stdlibs(exported_dir, swift_build_tree)


def export_headers(exported_dir, swift_source_tree, llvm_build_tree, swift_build_tree):
    print("exporting headers")
    # Assuming default checkout where LLVM sources are placed next to Swift sources
    llvm_source_tree = swift_source_tree.parent / 'llvm-project/llvm'
    clang_source_tree = swift_source_tree.parent / 'llvm-project/clang'
    clang_tools_build_tree = llvm_build_tree / 'tools/clang'
    header_dirs = [llvm_source_tree, clang_source_tree, swift_source_tree, llvm_build_tree, swift_build_tree,
                   clang_tools_build_tree]
    for h in header_dirs:
        copy_includes(h, exported_dir)


def zip_dir(src, tgt):
    tgt = get_tgt(tgt, f"swift-prebuilt-{get_platform()}.zip")
    print(f"compressing {src.name} to {tgt}")
    archive = shutil.make_archive(tgt, 'zip', src)
    print(f"created {archive}")


def main(opts):
    tmp = pathlib.Path('/tmp/llvm-swift')
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.mkdir(tmp)
    llvm_build_tree = next(opts.build_tree.glob("llvm-*"))
    swift_build_tree = next(opts.build_tree.glob("swift-*"))
    earlyswiftsyntax_build_tree = next(opts.build_tree.glob("earlyswiftsyntax-*"))
    configured = configure_dummy_project(tmp, prefixes=[llvm_build_tree, swift_build_tree,
                                                        earlyswiftsyntax_build_tree / "cmake" / "modules"])
    libs = get_libs(configured)

    exported = tmp / "exported"
    exported.mkdir()
    export_libs(exported, libs, swift_build_tree)
    export_headers(exported, opts.swift_source_tree, llvm_build_tree, swift_build_tree)
    export_sdk(exported / "sdk", opts.swift_source_tree, swift_build_tree)

    zip_dir(exported, opts.output)


if __name__ == "__main__":
    main(getoptions())
