################################################################################
# ecas 应用入口（L6–L7 业务层可执行）
# 模块：settings / config_watcher / node_manager / alert_engine / event_bus /
#       control_engine / audit_manager / main
# 阶段 0：最小骨架可执行（验证构建链路）；ecas-edge / ecas-central 后续拆分
################################################################################

add_executable(ecas
        main/main.cpp
)

target_link_libraries(ecas PRIVATE
        ecas::foundation
        ecas::acquisition
        ecas::communication
        ecas::storage
)
