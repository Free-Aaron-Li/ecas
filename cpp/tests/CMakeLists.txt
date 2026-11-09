################################################################################
# 测试（tests/）
# 阶段 0：Catch2 单元与 Spike 测试
################################################################################

find_package(Catch2 3 REQUIRED)

include(CTest)
include(Catch)

# Spike 测试集
add_executable(ecas_spike_tests
        spike/test_reflection_spike.cpp
)

target_include_directories(ecas_spike_tests PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/spike
)

target_link_libraries(ecas_spike_tests PRIVATE
        Catch2::Catch2WithMain
)

add_test(NAME ecas_spike_tests COMMAND ecas_spike_tests)
catch_discover_tests(ecas_spike_tests)

