cmake_minimum_required(VERSION 3.12.4)
project(codeql-swift-artifacts C CXX)

find_package(LLVM REQUIRED CONFIG)
find_package(Clang REQUIRED CONFIG)
find_package(Swift REQUIRED CONFIG)
find_package(SwiftSyntax REQUIRED CONFIG)

set(THREADS_PREFER_PTHREAD_FLAG YES)
include(FindThreads)

message("Using LLVM_CONFIG: ${Swift_CONFIG}")
message("Using Clang_CONFIG: ${LLVM_CONFIG}")
message("Using Swift_CONFIG: ${Clang_CONFIG}")

add_executable(codeql-swift-artifacts empty.cpp)
target_link_libraries(codeql-swift-artifacts PRIVATE LLVMSupport swiftFrontendTool swiftCompilerModules)

set (CODEQL_SWIFT_VERSION_MAJOR ${SWIFT_VERSION_MAJOR})
set (CODEQL_SWIFT_VERSION_MINOR ${SWIFT_VERSION_MINOR})
set (CODEQL_SWIFT_VERSION_PATCH ${SWIFT_VERSION_PATCH})

# TODO: these are now upstreamed
configure_file(${CMAKE_SOURCE_DIR}/CodeQLSwiftVersion.h.in
               ${SWIFT_BINARY_DIR}/include/swift/CodeQLSwiftVersion.h)

if(APPLE)
  execute_process(
    COMMAND xcrun -find swiftc
    OUTPUT_VARIABLE CODEQL_SWIFT_COMPILER
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
  execute_process(
    COMMAND xcrun -show-sdk-path
    OUTPUT_VARIABLE CODEQL_MACOS_SDK_PATH
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )

  # Add in the toolchain directory so we can grab compatibility libraries
  # Inspired by the Swift's CMakeLists
  get_filename_component(TOOLCHAIN_BIN_DIR ${CODEQL_SWIFT_COMPILER} DIRECTORY)
  get_filename_component(TOOLCHAIN_LIB_DIR "${TOOLCHAIN_BIN_DIR}/../lib/swift/macosx" ABSOLUTE)
  target_link_directories(codeql-swift-artifacts PUBLIC ${TOOLCHAIN_LIB_DIR})
  target_link_directories(codeql-swift-artifacts PUBLIC ${CODEQL_MACOS_SDK_PATH}/usr/lib/swift)
endif()
