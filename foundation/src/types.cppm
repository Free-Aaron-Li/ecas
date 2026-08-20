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
 * @file types.cppm
 * @brief 定义核心数据类型和错误码
 */
module;
#include <format>
#include <map>
#include <memory>
#include <source_location>
#include <string>
#include <variant>
#include <vector>

/**
 * @module foundation.types
 * @brief 核心类型模块
 */
export module foundation.types;

/**
 * @namespace ecas::foundation::types
 * @brief 核心类型定义命名空间
 *
 * 该命名空间包含系统中使用的核心数据类型，包括：
 * - ErrorCode：系统错误码枚举
 * - DataType：Modbus 寄存器数据类型枚举
 * - RegisterMeta：寄存器元信息结构
 * - MbpConfig：Modbus Poll 配置结构
 *
 * 这些类型被整个应用程序（包括配置加载、数据采集、解析等模块）广泛使用。
 */
export namespace ecas::foundation::types {
    /**
     * @enum ErrorCode
     * @brief 系统错误码定义
     *
     * 用于标识系统运行过程中的各种异常状态。
     */
    enum class ErrorCode {
        Success = 0,  ///< 成功

        // 文件与 I/O 相关
        FileNotFound,   ///< 文件未找到
        FileReadError,  ///< 文件读取错误

        // 解析与配置相关
        XmlParseFailed,        ///< XML 整体结构解析失败
        InvalidConfig,         ///< 配置内容不合法（通用）
        MissingRootElement,    ///< 缺少根元素 \<ModbusPoll\>
        MissingRequiredField,  ///< 缺少必需字段（如 SlaveID, Function 等）
        InvalidIntegerFormat,  ///< 整数字段格式错误（如非数字字符）
        InvalidAttribute,      ///< 属性值无效（如 idx 缺失或非整数）
        InvalidCellData,       ///< Cell 数据不完整（缺 Name 或 idx）

        // 连接与通信
        ConnectionFailed,  ///< 连接失败
        ReadTimeout,       ///< 读取超时

        // 数据存储
        StorageError,  ///< 数据存储错误

        // 通用
        IndexError,      ///< 寄存器地址越界
        DataParseError,  ///< 其它数据解析错误（兜底）
        UnknownError,    ///< 未知错误
    };

    /**
     * @brief 将错误码转换为对应的字符串描述
     *
     * 该函数用于将 ErrorCode 枚举值转换为人类可读的错误信息字符串，
     * 便于日志输出、调试和错误提示。
     *
     * @param code 需要转换的错误码（ErrorCode 枚举值）
     * @return std::string_view 错误码对应的描述字符串。
     *         如果错误码未知或不在已定义范围内，返回 "Unknown error"
     *
     * @note 返回的 string_view 指向静态字符串字面量，生命周期贯穿整个程序运行期
     */
    std::string_view
    error_code_description(const ErrorCode code) {
        using enum ErrorCode;
        switch (code) {
            case Success: return "Success";
            case FileNotFound: return "File not found";
            case FileReadError: return "File read error";
            case XmlParseFailed: return "XML parse failed";
            case InvalidConfig: return "Invalid configuration";
            case MissingRootElement: return "Missing root element <ModbusPoll>";
            case MissingRequiredField: return "Missing required field";
            case InvalidIntegerFormat: return "Invalid integer format";
            case InvalidAttribute: return "Invalid attribute";
            case InvalidCellData: return "Invalid cell data";
            case ConnectionFailed: return "Connection failed";
            case ReadTimeout: return "Read timeout";
            case StorageError: return "Storage error";
            case IndexError: return "Index out of bounds";
            case DataParseError: return "Data parse error";
            default: return "Unknown error";
        }
    }

    struct Error {
        ErrorCode              code;
        std::string            message;
        const char*            file;
        int                    line;
        const char*            function;
        std::unique_ptr<Error> cause;

        explicit Error(const ErrorCode code, std::string msg = {},
                       std::unique_ptr<Error>&&    prev = {},
                       const std::source_location& loc
                       = { std::source_location::current() }) :
            code(code), message(std::move(msg)), file(loc.file_name()),
            line(loc.line()), function(loc.function_name()),
            cause(std::move(prev)) {}

        explicit Error(const ErrorCode code, std::string msg, Error&& prev,
                       const std::source_location& loc
                       = { std::source_location::current() }) :
            code(code), message(std::move(msg)), file(loc.file_name()),
            line(loc.line()), function(loc.function_name()),
            cause(std::make_unique<Error>(std::move(prev))) {}

        /* 移动语义（默认生成） */
        Error(Error&&) noexcept            = default;
        Error& operator=(Error&&) noexcept = default;

        /* 禁止拷贝（保证唯一所有权） */
        Error(const Error&)            = delete;
        Error& operator=(const Error&) = delete;
    };

    /**
     * @brief 格式化错误链为多行字符串
     *
     * 该函数将 Error 对象及其嵌套的原因链（cause chain）
     * 格式化为人类可读的多行字符串，用于日志输出和调试。
     * 每一层错误显示为：
     * - 错误消息（如果为空则使用错误码的默认描述）
     * - 源代码位置（文件名、行号、简化的函数名）
     *
     * 输出格式示例：
     * ```
     * File read error (types.cppm:123 l.c.t.load_mbp_config)
     *   XML parse failed (mbp_parser.cppm:456 a.M.load)
     * ```
     *
     * @param err 要格式化的错误对象（常量引用）
     * @return std::string 格式化后的多行错误信息字符串，
     *         顶层错误在前，底层原因在后，每层通过缩进表示嵌套关系
     *
     * @note 函数名简化规则：
     *       - 去除返回类型和参数列表
     *       - 命名空间缩写为首字母，用点分隔（如 ecas::foundation::types ->
     * e.f.t）
     *       - 若无命名空间，直接使用函数名
     *
     * @note 预分配策略：
     *       - 错误帧向量预分配 8 层深度
     *       - 结果字符串预分配 (层数 × 96 + 64) 字节，减少动态内存分配
     *
     * @note Logger 未加载日志输出：
     *       - 当 Logger 未加载时，如：程序正在加载应用设置，应用设置失败，
     *       日志加载失败等情况下，将错误信息输出到标准错误流（stderr）
     */
    std::string
    format_error_chain(const Error& err) {
        /* 1. 收集所有帧（从顶层到底层） */
        std::vector<const Error*> frames{};
        frames.reserve(8);  ///< 常见预分配深度
        const Error* current{ &err };
        while (current) {
            frames.push_back(current);
            current = current->cause.get();
        }

        /* 2. 辅助：提取文件名（去除路径） */
        auto basename = [](const std::string_view path) -> std::string_view {
            const auto pos = path.find_last_of("/\\");
            return pos == std::string_view::npos ? path : path.substr(pos + 1);
        };

        /* 3. 辅助：简化函数名（去除参数、返回类型、将命名空间缩写为首字母） */
        auto simplify_function = [](std::string_view func) -> std::string {
            /* 3.1 去除参数列表（从第一个‘（’开始 */
            if (const auto paren{ func.find('(') };
                paren != std::string_view::npos) {
                func = func.substr(0, paren);
            }

            /* 3.2 去除返回类型（取最后一个空格之后的部分） */
            if (const auto space{ func.find_last_of(' ') };
                space != std::string_view::npos) {
                func = func.substr(space + 1);
            }

            /* 3.3 将命名空间缩写为首字母，用点连接 */
            std::string simplified{};
            size_t      start{ 0 };
            size_t      end{ 0 };
            bool        has_namespace{ false };
            while ((end = func.find("::", start)) != std::string_view::npos) {
                if (auto part = func.substr(start, end - start);
                    !part.empty()) {
                    if (part.front() == '{' || part.front() == '(') {
                        ///< 跳过匿名空间
                    } else {
                        if (has_namespace)
                            simplified += '.';
                        simplified += part[0];
                        has_namespace = true;
                    }
                }
                start = end + 2;
            }
            if (const auto last_part{ func.substr(start) };
                !last_part.empty()) {
                if (has_namespace)
                    simplified += '.';
                simplified += last_part;
            }

            /* 若无命名空间，直接使用原函数名 */
            if (simplified.empty()) {
                simplified = func;
            }

            return simplified;
        };

        /* 4. 构建结果字符串（预分配空间） */
        std::string result{};
        result.reserve(frames.size() * 96 + 64);

        /* 4. 从顶层（frames 的逆序）开始输出 */
        size_t level{ 0 };
        for (auto it = frames.rbegin(); it != frames.rend(); ++it, ++level) {
            const Error* error_ptr = *it;

            /* 1. 若不是顶层错误，则缩进表示嵌套层级 */
            if (level > 0) {
                result.append((level + 1) * 4, ' ');
            }

            /* 2. 合并错误码描述和自定义消息 */
            std::string full_msg;
            full_msg.reserve(64 + error_ptr->message.size());

            /* 2.1 始终显示错误码编号和描述 */
            full_msg += std::format("[{}] {} | ",
                                    static_cast<int8_t>(error_ptr->code),
                                    error_code_description(error_ptr->code));

            /* 2.2 如果有自定义消息，追加附加信息 */
            if (!error_ptr->message.empty()) {
                full_msg += std::format("{}", error_ptr->message);
            }

            /* 生成位置字符串（与 Logger 格式一致） */
            auto file = basename(error_ptr->file);
            auto func = simplify_function(error_ptr->function);
            result += std::format("{} ({}:{} {})", full_msg, file,
                                  error_ptr->line, func);
            result.push_back('\n');
        }
        /* 移除最后一个多余的换行 */
        if (!result.empty() && result.back() == '\n') {
            result.pop_back();
        }
        return result;
    }

    /**
     * @enum DataType
     * @brief 寄存器数据类型
     */
    enum class DataType {
        Unknown,   ///< 未知
        Int16,     ///< 16位有符号整数
        UInt16,    ///< 16位无符号整数
        Int32,     ///< 32位有符号整数
        UInt32,    ///< 32位无符号整数
        Int64,     ///< 64位有符号整数
        UInt64,    ///< 64位无符号整数
        BFloat32,  ///< 32位浮点数（大端）
        LFloat32,  ///< 32位浮点数（小端）
        Double64,  ///< 64位浮点数 (双精度)
        Boolean,   ///< 布尔值
        String,    ///< 字符串
        Bitmask,   ///< 位掩码
    };

    /**
     * @struct RegisterMeta
     * @brief 寄存器的完整元信息
     *
     * 该结构体用于描述 Modbus 寄存器的完整元数据信息，包括：
     * - 寄存器标识信息（名称、描述）
     * - 数据类型定义（如 Int16、Float32 等）
     * - 物理量转换参数（单位、缩放系数、偏移量）
     *
     * 物理值的计算公式为：物理值 = 原始值 × scale + offset
     *
     * 该结构体通常从 MBP 配置文件的 \<Cell\> 节点解析得到，
     * 并存储在 MbpConfig::registerMap 中，以寄存器索引为键进行映射。
     */
    struct RegisterMeta {
        std::string name{};  ///< 寄存器名称（必需），用于标识寄存器的含义
        std::string unit{};  ///< 物理单位（可选），表示测量值的单位
        DataType    dataType{
            DataType::Unknown
        };  ///< 数据类型（必需），定义如何解析寄存器的原始字节数据
        double scale{
            1.0
        };  ///< 缩放系数（默认 1.0），用于将原始值转换为物理值的乘数
        double offset{
            0.0
        };  ///< 偏移量（默认 0.0），转换后的加法修正值（物理值 = 原始值 × scale
            ///< + offset）
        std::string description{};  ///< 描述信息（可选），提供关于该寄存器的详细说明或备注
    };

    /**
     * @struct MbpConfig
     * @brief Modbus 配置信息，从 MBP 文件中解析得到
     */
    struct MbpConfig {
        int32_t slaveId{ 0 };   ///< Modbus 从站 ID
        int32_t function{ 3 };  ///< Modbus 功能码，默认为 3（读保持寄存器）
        int32_t address{ 0 };   ///< 起始寄存器地址
        int32_t quantity{ 0 };  ///< 要读取的寄存器数量
        std::map<int32_t, RegisterMeta>
                  registerMap;  ///< 寄存器索引到元信息的映射表
    };

    /**
     * @struct IniConfig
     * @brief INI 配置文件信息
     *
     * 该结构体用于存储从 INI 配置文件中解析得到的配置信息。
     * INI 文件通常由多个节（section）组成，每个节包含若干键值对。
     */
    struct IniConfig {
        std::map<std::string, std::map<std::string, std::string>> sections;
    };

    /**
     * @struct JsonConfig
     * @brief JSON 配置文件信息
     *
     * 该结构体用于存储从 JSON 配置文件中解析得到的配置信息。
     * JSON 文件支持嵌套结构，可以包含对象、数组等复杂数据类型。
     *
     * 当前实现采用简化的键值对映射方式，支持基本的配置存储需求。
     * 对于更复杂的 JSON 结构解析，建议使用专用的 JSON 库（如 nlohmann/json）。
     */
    struct JsonConfig {
        std::map<std::string, std::string>
                  data;  ///< 键值对映射表，存储 JSON 配置的扁平化表示
    };

    /**
     * @typedef ConfigVariant
     * @brief 配置文件类型变体，支持多种配置文件格式
     *
     * 使用 std::variant 定义一个类型变体，可以容纳不同类型的配置文件信息。
     * 目前支持 MbpConfig、IniConfig 和 JsonConfig 三种类型。
     */
    using ConfigVariant = std::variant<MbpConfig, IniConfig, JsonConfig>;
}  // namespace ecas::foundation::types
