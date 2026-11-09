################################################################################
# ECAS 核心源码聚合（src/）
# 分层（L1–L8 单向依赖，依赖仅向下）：
#   foundation → acquisition → communication → storage → application
#   ui（L8，Qt 6）待 Qt 就绪后挂载
################################################################################

add_subdirectory(foundation)
add_subdirectory(acquisition)
add_subdirectory(communication)
add_subdirectory(storage)
add_subdirectory(application)
