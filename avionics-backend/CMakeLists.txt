cmake_minimum_required(VERSION 3.20)
project(avionics_backend VERSION 0.6.0 LANGUAGES C CXX)

set(AVIONICS_BUILD_REVISION "" CACHE STRING "Commit identity when building a source export without Git")
if(NOT AVIONICS_BUILD_REVISION)
    find_package(Git REQUIRED)
    execute_process(COMMAND "${GIT_EXECUTABLE}" rev-parse HEAD WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
        RESULT_VARIABLE revision_result OUTPUT_VARIABLE AVIONICS_BUILD_REVISION OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(NOT revision_result EQUAL 0)
        message(FATAL_ERROR "Cannot determine source identity; supply AVIONICS_BUILD_REVISION for a source export")
    endif()
    execute_process(COMMAND "${GIT_EXECUTABLE}" status --porcelain WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
        RESULT_VARIABLE status_result OUTPUT_VARIABLE source_changes)
    if(NOT status_result EQUAL 0)
        message(FATAL_ERROR "Cannot inspect source state")
    endif()
    if(source_changes)
        string(APPEND AVIONICS_BUILD_REVISION "-dirty")
    endif()
endif()
configure_file(include/core/build_info.hpp.in generated/core/build_info.hpp @ONLY)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
find_package(Threads REQUIRED)

add_library(project_options INTERFACE)
target_include_directories(project_options INTERFACE "${PROJECT_SOURCE_DIR}/include" "${PROJECT_BINARY_DIR}/generated")
target_compile_options(project_options INTERFACE
    $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall;-Wextra;-Wpedantic;-Wconversion;-Wshadow>)

add_library(bus_archive STATIC src/storage/archive.cpp src/storage/identity.cpp)
target_link_libraries(bus_archive PUBLIC project_options)
if(WIN32)
    target_link_libraries(bus_archive PUBLIC bcrypt)
else()
    find_package(OpenSSL REQUIRED COMPONENTS Crypto)
    target_link_libraries(bus_archive PUBLIC OpenSSL::Crypto)
endif()

add_library(bus_core STATIC
    src/platform/shared_library.cpp
    src/platform/worker_process.cpp
    src/plugin/plugin_manager.cpp
    src/bus/bus_manager.cpp
    src/bus/capture_adapter.cpp
    src/pipeline/frame_pipeline.cpp
    src/decoder/simulated_decoder.cpp
    src/decoder/configuration.cpp
    src/decoder/dictionary_decoder.cpp
    src/decoder/mil1553b.cpp
    src/analysis/processing.cpp
    src/control_console.cpp
    src/storage/jsonl_analysis.cpp
    src/storage/session_journal.cpp
    src/analysis/rule_analyzer.cpp)
target_link_libraries(bus_core PUBLIC bus_archive project_options Threads::Threads ${CMAKE_DL_LIBS})
if(WIN32)
    target_compile_definitions(bus_core PRIVATE _WIN32_WINNT=0x0A00 WINVER=0x0A00)
    target_link_libraries(bus_core PUBLIC shell32)
endif()

function(add_bus_plugin name source)
    add_library(${name} MODULE ${source})
    target_link_libraries(${name} PRIVATE project_options Threads::Threads)
    target_compile_definitions(${name} PRIVATE BUS_PLUGIN_BUILD)
    set_target_properties(${name} PROPERTIES PREFIX "" CXX_VISIBILITY_PRESET hidden
        VISIBILITY_INLINES_HIDDEN YES)
endfunction()
add_bus_plugin(bus_simulated plugins/simulated/simulated_plugin.cpp)
add_bus_plugin(bus_replay plugins/replay/replay_plugin.cpp)
add_bus_plugin(bus_mil1553b_simulated plugins/mil1553b_simulated/mil1553b_plugin.cpp)
target_link_libraries(bus_replay PRIVATE bus_archive)

add_executable(bus_backend src/main.cpp)
target_link_libraries(bus_backend PRIVATE bus_core)
add_executable(bus_capture_worker src/capture_worker_main.cpp)
target_link_libraries(bus_capture_worker PRIVATE bus_core)
add_dependencies(bus_backend bus_capture_worker)
add_executable(bus_analyze src/analyze_main.cpp)
target_link_libraries(bus_analyze PRIVATE bus_core)

include(CTest)
option(AVIONICS_TEST_QT_FRONTEND "Run Qt for Python desktop/backend integration tests" OFF)
if(BUILD_TESTING)
    add_bus_plugin(bus_fault tests/fault_plugin.cpp)
    add_bus_plugin(bus_hang_load tests/fault_plugin.cpp)
    target_compile_definitions(bus_hang_load PRIVATE BUS_TEST_HANG_LOAD)
    add_executable(capture_isolation_tests tests/capture_isolation.cpp)
    target_link_libraries(capture_isolation_tests PRIVATE bus_core)
    add_dependencies(capture_isolation_tests bus_capture_worker)
    add_test(NAME capture_isolation COMMAND capture_isolation_tests $<TARGET_FILE:bus_simulated>
        $<TARGET_FILE:bus_fault> $<TARGET_FILE:bus_hang_load> "${CMAKE_BINARY_DIR}/test-output")
    set_tests_properties(capture_isolation PROPERTIES TIMEOUT 20)
    add_executable(archive_integrity_tests tests/archive_integrity.cpp)
    target_link_libraries(archive_integrity_tests PRIVATE bus_archive Threads::Threads)
    add_test(NAME archive_integrity COMMAND archive_integrity_tests
        "${CMAKE_BINARY_DIR}/test-output")
    add_executable(configuration_benchmark tests/configuration_benchmark.cpp)
    target_link_libraries(configuration_benchmark PRIVATE bus_core)
    add_bus_plugin(bus_incompatible tests/incompatible_plugin.cpp)
    add_library(c_header_check OBJECT tests/c_header_check.c)
    target_link_libraries(c_header_check PRIVATE project_options)
    add_executable(bus_tests tests/integration.cpp)
    target_link_libraries(bus_tests PRIVATE bus_core)
    add_dependencies(bus_tests bus_capture_worker)
    add_test(NAME backend_integration COMMAND bus_tests
        $<TARGET_FILE:bus_simulated> $<TARGET_FILE:bus_replay>
        $<TARGET_FILE:bus_incompatible> "${CMAKE_BINARY_DIR}/test-output")
    set_tests_properties(backend_integration PROPERTIES TIMEOUT 30)
    add_executable(processing_tests tests/processing.cpp)
    target_link_libraries(processing_tests PRIVATE bus_core)
    add_test(NAME processing_integration COMMAND processing_tests
        "${PROJECT_SOURCE_DIR}/config/correlation-demo.ini" "${CMAKE_BINARY_DIR}/processing-output")
    set_tests_properties(processing_integration PROPERTIES TIMEOUT 30)
    add_executable(protocol_contract_tests tests/protocol_contract.cpp)
    target_link_libraries(protocol_contract_tests PRIVATE bus_core)
    add_test(NAME protocol_contract COMMAND protocol_contract_tests)
    add_executable(mil1553b_tests tests/mil1553b.cpp)
    target_link_libraries(mil1553b_tests PRIVATE bus_core)
    add_test(NAME mil1553b_protocol COMMAND mil1553b_tests "${PROJECT_SOURCE_DIR}/config/mil1553b-demo.ini" "${CMAKE_BINARY_DIR}/1553b-test-output")
    if(AVIONICS_TEST_QT_FRONTEND)
        find_package(Python3 REQUIRED COMPONENTS Interpreter)
        add_test(NAME qt_frontend COMMAND "${Python3_EXECUTABLE}" -B
            "${PROJECT_SOURCE_DIR}/frontend/test_frontend.py" --bin-dir "$<TARGET_FILE_DIR:bus_backend>"
            --output "${CMAKE_BINARY_DIR}/frontend-test-output")
        set_tests_properties(qt_frontend PROPERTIES TIMEOUT 120
            ENVIRONMENT "QT_QPA_PLATFORM=offscreen;PYTHONDONTWRITEBYTECODE=1")
        add_test(NAME qt_relations COMMAND "${Python3_EXECUTABLE}" -B
            "${PROJECT_SOURCE_DIR}/frontend/test_relations.py")
        set_tests_properties(qt_relations PROPERTIES ENVIRONMENT "PYTHONDONTWRITEBYTECODE=1")
        add_test(NAME qt_workbench COMMAND "${Python3_EXECUTABLE}" -B -m ground_station.release_check
            --bin-dir "$<TARGET_FILE_DIR:bus_backend>" --output "${CMAKE_BINARY_DIR}/workbench-test-output")
        set_tests_properties(qt_workbench PROPERTIES TIMEOUT 120 WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}/frontend"
            ENVIRONMENT "QT_QPA_PLATFORM=offscreen;PYTHONDONTWRITEBYTECODE=1")
    endif()
endif()
