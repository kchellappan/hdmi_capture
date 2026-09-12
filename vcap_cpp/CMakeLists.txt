# SPDX-License-Identifier: MIT
#
# Builds the C++ capture client. Links nothing but pthread: V4L2 is ioctls on a character
# device and the kernel's own header supplies the ABI, so there is no library to find and
# nothing to install before building.
cmake_minimum_required(VERSION 3.16)
project(vcap_cpp CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra)

find_package(Threads REQUIRED)

add_library(vcap_cpp STATIC
  src/frame.cpp
  src/device.cpp
  src/source.cpp
  src/writer.cpp
  src/manifest.cpp
)
target_include_directories(vcap_cpp PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(vcap_cpp PUBLIC Threads::Threads)

add_executable(vcap-record-cpp tools/vcap_record.cpp)
target_link_libraries(vcap-record-cpp PRIVATE vcap_cpp)

enable_testing()
add_executable(test_index_format tests/test_index_format.cpp)
target_link_libraries(test_index_format PRIVATE vcap_cpp)
add_test(NAME index_format COMMAND test_index_format)

add_executable(write_synthetic tests/write_synthetic.cpp)
target_link_libraries(write_synthetic PRIVATE vcap_cpp)

add_executable(test_jpeg tests/test_jpeg.cpp)
target_link_libraries(test_jpeg PRIVATE vcap_cpp)
add_test(NAME jpeg COMMAND test_jpeg)
