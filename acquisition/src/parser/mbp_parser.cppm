// Copyright (C) 2026 Aaron <communicate_aaron@outlook.com>
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

/**
 * @file mbp_parser.cppm
 * @brief MBP (Modbus Poll) XML 配置文件解析器模块定义文件
 *
 * 本文件定义了用于解析 Modbus Poll 软件导出的 XML 格式配置文件的模块接口。
 * 该模块提供 MbpParser 类及相关辅助函数，用于从 .mbp 文件中提取 Modbus 通信参数
 * （从站 ID、功能码、地址、数量）以及寄存器元信息（名称、索引等）。
 */
module;

#include <charconv>
#include <expected>
#include <format>
#include <map>
#include <optional>
#include <pugixml.hpp>
#include <ranges>
#include <string>
#include <string_view>
#include <vector>

/**
 * @module acquisition.parser.mbp_parser
 * @brief Modbus Poll (.mbp) XML 配置文件解析模块
 *
 * 本模块提供对 Modbus Poll 软件导出的 XML 格式配置文件的解析功能，
 * 从中提取 Modbus 通信参数（从站 ID、功能码、地址、数量）以及寄存器元信息。
 *
 * @see std::from_chars std::from_chars std::make_error_code
 */
export module acquisition.parser.mbp_parser;
import foundation.types;

namespace ecas::acquisition {
    using foundation::types::DataType;
    using foundation::types::Error;
    using foundation::types::ErrorCode;
    using foundation::types::MbpConfig;
    using foundation::types::RegisterMeta;

    /**
     * @brief 工具函数
     */
    namespace {
        /**
         * @brief 将字符串解析为整数
         * @param str 要解析的字符串视图
         * @return 解析成功返回整数，失败返回 std::nullopt
         */
        [[nodiscard]] std::optional<int32_t>
        parse_stoi(const std::string_view str) noexcept {
            /* 1. 跳过前导空白字符，模拟 std::stoi 行为 */
            if (const auto first_non_space{
                          str.find_first_not_of(" \t\n\r\f\v") };
                first_non_space == std::string_view::npos) {
                return std::nullopt;
            }

            /* 2. 使用 from_chats 进行无异常、零拷贝解析 */
            int32_t           value{ 0 };
            const char* const begin{ str.data() };
            const char* const end{ begin + str.size() };

            /* 3. 成功操作：无错误且消费了全部字符（不允许多余字符） */
            if (auto [ptr, ec]{ std::from_chars(begin, end, value) };
                ec == std::errc() && ptr == end) {
                return value;
            }

            /* 4. 解析失败，记录原始输入 */
            return std::nullopt;
        }

        /**
         * @brief 将 Modbus Poll 格式代码映射到数据类型
         * @param code 格式代码
         * @return 对应的数据类型
         * TODO 此处需对照官方文档修改
         */
        DataType
        map_format_code_to_datatype(const int32_t code) noexcept {
            switch (code) {
                case 1: return DataType::Int16;
                case 0:
                case 2: return DataType::UInt16;
                case 3:
                case 4: return DataType::Int32;
                case 5: return DataType::BFloat32;
                case 6: return DataType::LFloat32;
                case 8: return DataType::Double64;
                case 7: return DataType::String;  ///< 通常为 ASCII 类型
                case 16: return DataType::UInt16;
                default: return DataType::Unknown;
            }
        }

        /**
         * @brief 获取数据类型占用的 16 位寄存器个数
         * @param dt 数据类型
         * @return 寄存器跨度
         */
        int8_t
        data_type_register_span(const DataType dt) {
            switch (dt) {
                case DataType::Int16:
                case DataType::UInt16:
                case DataType::Boolean:
                case DataType::Bitmask: return 1;
                case DataType::Int32:
                case DataType::UInt32:
                case DataType::BFloat32: return 2;
                case DataType::Double64: return 4;
                case DataType::String:  ///< 保守假设为1个寄存器，实际需求根据配置长度
                default: return 1;
            }
        }

        /**
         * @brief 从 XML 节点中解析格式代码列表
         *
         * 该函数从给定的 XML 数据节点中提取 `<Formats>` 子节点，并遍历其所有
         * `<F>` 子元素， 解析每个元素的 `f` 属性值为整数格式代码。
         *
         * @param data_node XML 数据节点，预期包含 `<Formats>` 子节点
         * @return 成功时返回格式代码的整数向量；
         *         若 `<Formats>` 节点不存在或属性解析失败返回包含具体原因的
         * Error
         */
        [[nodiscard]] std::expected<std::vector<int32_t>, Error>
        parse_formats(const pugi::xml_node& data_node) noexcept {
            std::vector<int32_t> format_codes{};
            const auto           formats_node{ data_node.child("Formats") };
            if (!formats_node) {
                return std::unexpected(Error{
                          ErrorCode::MissingRequiredField,
                          "missing required <Formats> element under <Data>" });
            }

            size_t index{ 0 };
            for (const auto& f_node: formats_node.children("F")) {
                /* 具体格式：<F f="5" v="8280.23"/> */
                auto attr{ f_node.attribute("f") };
                if (!attr) {
                    return std::unexpected(Error{
                              ErrorCode::InvalidAttribute,
                              std::format("missing required 'f' attribute in "
                                          "<Formats><F> element at index {}",
                                          index) });
                }
                auto val_opt{ parse_stoi(attr.as_string()) };
                if (!val_opt) {
                    return std::unexpected(Error{
                              ErrorCode::InvalidIntegerFormat,
                              std::format("invalid integer format for 'f' "
                                          "attribute in <Formats><F>: '{}' at "
                                          "index {}",
                                          attr.as_string(), index) });
                }
                format_codes.push_back(*val_opt);
                ++index;
            }
            return format_codes;
        }

        /**
         * @brief 从 XML 节点中解析单元格数据（寄存器名称映射）
         *
         * 该函数从给定的 XML 数据节点中提取 `<CellData>` 子节点，并遍历其所有
         * `<Cell>` 子元素，解析每个元素的索引属性 `idx` 和名称子节点 `<Name>`，
         * 构建索引到名称的映射表。
         *
         * @param data_node XML 数据节点，预期包含 `<CellData>` 子节点
         * @param warnings <Cell> 读取警告信息
         * @return 成功时返回索引到名称字符串视图的映射表（idx:name）；
         *         若 `<CellData>` 节点不存在或解析失败返回包含具体原因的 Error
         */
        [[nodiscard]] std::expected<std::map<int32_t, std::string_view>, Error>
        parse_cell_data(const pugi::xml_node&     data_node,
                        std::vector<std::string>* warnings = nullptr) noexcept {
            std::map<int32_t, std::string_view> name_map{};
            const auto cell_node_data{ data_node.child("CellData") };
            if (!cell_node_data) {
                return std::unexpected(Error{
                          ErrorCode::InvalidCellData,
                          "missing required <CellData> element under <Data>" });
            }

            size_t cell_index{ 0 };
            for (const auto& child: cell_node_data.children()) {
                /* 若读取到的不是 Cell 节点 */
                if (std::string_view(child.name()) != "Cell") {
                    if (warnings) {
                        warnings->push_back(
                                  std::format("Skipping unknown element "
                                              "\"<{}>\" under <CellData>",
                                              child.name()));
                    }
                    continue;  ///< 跳过非 Cell 节点
                }

                /*
                 * 读取到正常 Cell 节点如下：
                <Cell idx="0">
                    <Name>800T 总表</Name>
                </Cell>
                 */
                auto idx_attr{ child.attribute("idx") };
                if (!idx_attr) {
                    return std::unexpected(Error{
                              ErrorCode::InvalidAttribute,
                              std::format(
                                        "missing required 'idx' attribute in "
                                        "<CellData><Cell> element at index {}",
                                        cell_index) });
                }
                auto idx_opt{ parse_stoi(idx_attr.as_string()) };
                if (!idx_opt) {
                    return std::unexpected(Error{
                              ErrorCode::InvalidIntegerFormat,
                              std::format(
                                        "invalid integer format for 'idx' "
                                        "attribute in <Cell>: '{}' at index {}",
                                        idx_attr.as_string(), cell_index) });
                }

                auto node_name{ child.child("Name") };
                if (!node_name) {
                    return std::unexpected(
                              Error{ ErrorCode::MissingRequiredField,
                                     std::format("missing required <Name> "
                                                 "element in <Cell idx=\"{}\">",
                                                 *idx_opt) });
                }
                name_map[*idx_opt]
                          = std::string_view(node_name.text().as_string());
                ++cell_index;
            }
            return name_map;
        }
    }  // namespace

    /**
     * @class MbpParser
     * @brief Modbus Poll (.mbp) 导出 XML 文件的解析类
     */
    export class MbpParser {
    public:
        /**
         * @brief 加载并解析 MBP XML 内容
         * @param xml_content XML 格式的字符串内容
         * @param warning <Cell> 读取警告信息
         * @return 成功返回填充好的 MbpConfig 结构体，失败返回包含详细上下文的
         * Error
         */
        [[nodiscard]] static std::expected<MbpConfig, Error>
        load(const std::string_view    xml_content,
             std::vector<std::string>* warning = nullptr) noexcept {
            /* 1. 解析 XML */
            pugi::xml_document doc{};
            auto               parse_result{ doc.load_buffer(xml_content.data(),
                                                             xml_content.size()) };
            if (!parse_result) {
                return std::unexpected(
                          Error{ ErrorCode::XmlParseFailed,
                                 std::format("XML parse error: {} (offset: {})",
                                             parse_result.description(),
                                             parse_result.offset) });
            }

            /* 2. 获取根节点 */
            auto root{ doc.child("ModbusPoll") };
            if (!root) {
                return std::unexpected(Error{
                          ErrorCode::MissingRootElement,
                          "missing required root element <ModbusPoll>" });
            }

            /* 3. 辅助：从根节点或指定父节点下获取整数子节点 */
            auto get_child_int = [&root](const std::string_view parent_tag,
                                         const std::string_view child_tag)
                      -> std::expected<int32_t, Error> {
                const auto parent{ parent_tag.empty()
                                             ? root
                                             : root.child(parent_tag.data()) };
                if (!parent) {
                    return std::unexpected(Error{
                              ErrorCode::MissingRequiredField,
                              std::format(
                                        "missing required parent element <{}>",
                                        parent_tag) });
                }

                const auto child{ parent.child(child_tag.data()) };
                if (!child) {
                    const auto path{ parent_tag.empty()
                                               ? std::format("<{}>", child_tag)
                                               : std::format("<{}><{}>",
                                                             parent_tag,
                                                             child_tag) };
                    return std::unexpected(Error{
                              ErrorCode::MissingRequiredField,
                              std::format("missing required field element {}",
                                          path) });
                }

                /* child.text() ：<Func>3</Func>，获取其中的“3” */
                const std::string_view text{ child.text().as_string() };
                auto                   val_opt{ parse_stoi(text) };
                if (!val_opt) {
                    const auto path{ parent_tag.empty()
                                               ? std::format("<{}>", child_tag)
                                               : std::format("<{}><{}>",
                                                             parent_tag,
                                                             child_tag) };
                    return std::unexpected(Error{
                              ErrorCode::InvalidIntegerFormat,
                              std::format("invalid integer value for {}: '{}'",
                                          path, text) });
                }
                return *val_opt;
            };

            /* 4. 读取必填字段 */
            auto slave_id_exp{ get_child_int("", "SlaveID") };
            if (!slave_id_exp) {
                return std::unexpected(std::move(slave_id_exp).error());
            }
            auto function_exp{ get_child_int("Data", "Function") };
            if (!function_exp) {
                return std::unexpected(std::move(function_exp).error());
            }
            auto address_exp{ get_child_int("Data", "Address") };
            if (!address_exp) {
                return std::unexpected(std::move(address_exp).error());
            }
            auto quantity_exp{ get_child_int("Data", "Quantity") };
            if (!quantity_exp) {
                return std::unexpected(std::move(quantity_exp).error());
            }

            /* 取出正确值 */
            auto slave_id{ *slave_id_exp };
            auto function{ *function_exp };
            auto address{ *address_exp };
            auto quantity{ *quantity_exp };

            if (quantity <= 0) {
                return std::unexpected(
                          Error{ ErrorCode::InvalidConfig,
                                 std::format("invalid register quantity: {} "
                                             "(must be greater than 0)",
                                             quantity) });
            }

            /* 5. 获取 Data 节点（用于后续解析） */
            auto data_node{ root.child("Data") };
            if (!data_node) {
                return std::unexpected(Error{
                          ErrorCode::MissingRequiredField,
                          "missing required <Data> section in <ModbusPoll>" });
            }

            /* 6. 解析 Formats */
            auto format_codes_exp{ parse_formats(data_node) };
            if (!format_codes_exp) {
                return std::unexpected(std::move(format_codes_exp).error());
            }
            auto format_codes{ std::move(*format_codes_exp) };

            /* 7. 解析 CellData */
            auto name_map_exp{ parse_cell_data(data_node, warning) };
            if (!name_map_exp) {
                return std::unexpected(std::move(name_map_exp).error());
            }
            auto name_map{ std::move(*name_map_exp) };

            /* 8. 构建 registerMap（铺满全部地址 0~quantity-1） */
            std::map<int32_t, RegisterMeta> register_map{};
            for (auto addr{ 0 }; addr < quantity; ++addr) {
                RegisterMeta meta{};

                /* 名称：优先从 name_map 中获取，否则生成默认 */
                if (auto it_name{ name_map.find(addr) };
                    it_name != name_map.end()) {
                    meta.name = it_name->second;  ///< second 表示键值对中的值
                } else {
                    meta.name = "Reg_" + std::to_string(addr);
                }

                /* 数据类型：从 format_codes 中获取（若存在） */
                if (static_cast<size_t>(addr) < format_codes.size()) {
                    meta.dataType
                              = map_format_code_to_datatype(format_codes[addr]);
                } else {
                    meta.dataType = DataType::Unknown;
                }

                /* MBP 文件没有 Scale/Offset/Unit/Description，保留默认 */
                meta.scale       = 1.0;
                meta.offset      = 0.0;
                meta.unit        = "";
                meta.description = "";

                register_map.emplace(addr, std::move(meta));
            }

            /* 9. 跨度校验：对 name_map 中的每个起始地址，检查其占用是否越界 */
            for (const auto& idx: name_map | std::views::keys) {
                if (idx < 0 || idx >= quantity) {
                    return std::unexpected(Error{
                              ErrorCode::IndexError,
                              std::format("register index {} in <CellData> "
                                          "exceeds configured quantity range "
                                          "[0, {})",
                                          idx, quantity) });
                }
                auto it = register_map.find(idx);
                if (it == register_map.end()) {
                    /* 理论上不会发生（因为已经铺满），但防御性处理 */
                    continue;
                }
                const auto& meta = it->second;
                if (auto span = data_type_register_span(meta.dataType);
                    idx + span > quantity) {
                    /* 越界：返回配置无效错误 */
                    return std::unexpected(Error{
                              ErrorCode::IndexError,
                              std::format("register '{}' (index: {}, dataType "
                                          "span: {}) exceeds total quantity {}",
                                          meta.name, idx, span, quantity) });
                }
            }

            /* 10. 组装最终配置 */
            MbpConfig config;
            config.slaveId     = slave_id;
            config.function    = function;
            config.address     = address;
            config.quantity    = quantity;
            config.registerMap = std::move(register_map);
            return config;
        }
    };
}  // namespace ecas::acquisition
