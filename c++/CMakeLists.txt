cmake_minimum_required(VERSION 3.16)
project(MiniPlayerCpp VERSION 1.0.0 LANGUAGES CXX)

# -----------------------------------------------------------------------------
#  MiniPlayer (C++/Qt5) —— 多线程 + 缓存队列 + 变速播放 的 FFmpeg 播放器
#
#  构建（MinGW + Qt5.12，见 build.ps1）：
#     cmake -G "MinGW Makefiles" -DCMAKE_PREFIX_PATH=D:/Qt/Qt5.12.9/5.12.9/mingw73_64 ..
#     cmake --build . -j
# -----------------------------------------------------------------------------
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_INCLUDE_CURRENT_DIR ON)
set(CMAKE_AUTOMOC ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

find_package(Qt5 5.12 REQUIRED COMPONENTS Core Gui Widgets Multimedia)

# 可选的 FFmpeg 开发库（libav*）：有就用进程内解码；没有就走 ffmpeg.exe 子进程 / 合成源
find_path(AVCODEC_INCLUDE_DIR libavcodec/avcodec.h)
find_library(AVCODEC_LIBRARY NAMES avcodec)
find_library(AVFORMAT_LIBRARY NAMES avformat)
find_library(AVUTIL_LIBRARY NAMES avutil)

set(HAVE_FFMPEG_LIBAV OFF)
if(AVCODEC_INCLUDE_DIR AND AVCODEC_LIBRARY AND AVFORMAT_LIBRARY AND AVUTIL_LIBRARY)
    set(HAVE_FFMPEG_LIBAV ON)
    message(STATUS "找到 FFmpeg 开发库：${AVCODEC_INCLUDE_DIR}")
else()
    message(STATUS "未找到 FFmpeg 开发库 -> 走 ffmpeg.exe 子进程 / 内置合成源（管线功能不受影响）")
endif()

set(CORE_SOURCES
    src/core/media_clock.cpp
    src/core/time_stretch.cpp
    src/core/synthetic_decoder.cpp
    src/core/child_process.cpp
    src/core/ffmpeg_cli_decoder.cpp
    src/core/decoder_factory.cpp
    src/core/player.cpp
    src/core/selftest.cpp

    src/threads/reader_thread.cpp
    src/threads/video_renderer.cpp
    src/threads/audio_renderer.cpp

    src/ui/video_widget.cpp
    src/ui/main_window.cpp
    src/ui/style.cpp
)

set(CORE_HEADERS
    src/core/config.h
    src/core/media_clock.h
    src/core/frame.h
    src/core/frame_queue.h
    src/core/time_stretch.h
    src/core/decoder.h
    src/core/synthetic_decoder.h
    src/core/child_process.h
    src/core/ffmpeg_cli_decoder.h
    src/core/decoder_factory.h
    src/core/player.h
    src/core/selftest.h

    src/threads/stoppable_thread.h
    src/threads/reader_thread.h
    src/threads/video_renderer.h
    src/threads/audio_renderer.h

    src/ui/video_widget.h
    src/ui/main_window.h
    src/ui/style.h
)

# 核心做成静态库：GUI 版和控制台版共用同一份编译产物
add_library(miniplayer_core STATIC ${CORE_SOURCES} ${CORE_HEADERS})
target_include_directories(miniplayer_core PUBLIC src)
target_link_libraries(miniplayer_core PUBLIC
    Qt5::Core Qt5::Gui Qt5::Widgets Qt5::Multimedia
)
if(HAVE_FFMPEG_LIBAV)
    target_compile_definitions(miniplayer_core PUBLIC MINIPLAYER_HAVE_LIBAV=1)
    target_include_directories(miniplayer_core PUBLIC ${AVCODEC_INCLUDE_DIR})
endif()

# 把源码目录编进去：这样从 build/xxx 里运行也能找到 tools/ffmpeg/bin/ffmpeg.exe
target_compile_definitions(miniplayer_core PRIVATE
    MINIPLAYER_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}")
if(MSVC)
    target_compile_options(miniplayer_core PRIVATE /W3 /utf-8)
else()
    target_compile_options(miniplayer_core PRIVATE -Wall -Wextra -Wno-unused-parameter)
endif()

# GUI 版：无控制台窗口
add_executable(miniplayer WIN32 src/main.cpp)
target_link_libraries(miniplayer PRIVATE miniplayer_core)

# 控制台版：跑 --selftest 时能看到输出
add_executable(miniplayer_console src/main.cpp)
target_link_libraries(miniplayer_console PRIVATE miniplayer_core)

# 纯逻辑单元测试：只测队列/时钟/变速拼接，不开窗口、不开声卡、不解码文件
add_executable(miniplayer_unittest tests/unit_tests.cpp)
target_link_libraries(miniplayer_unittest PRIVATE miniplayer_core)

# 源码里有中文注释/字符串，必须显式告诉 MSVC 用 UTF-8，否则 C4819 且中文可能乱码
if(MSVC)
    target_compile_options(miniplayer PRIVATE /utf-8)
    target_compile_options(miniplayer_console PRIVATE /utf-8)
    target_compile_options(miniplayer_unittest PRIVATE /utf-8)
endif()
