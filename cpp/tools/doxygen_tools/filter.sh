#!/bin/bash
#
# Copyright (C) 2026 Aaron <communicate_aaron@outlook.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# 1. 处理 ==text== 为 @mark{text}
# 2. 将 **text** 转换为 <b>text</b>，处理 * * 为 <i> </i>
# 3. 将 <!-- id:xxx --> \n # Title 转换为 生成页面定义 + 原标题（用于建立页面层级）：\n#    @page <id> <Title>  +  # Title
# 4. 将 [text](../path/filename.cpp) 转换为 @ref filename.cpp "text"
# 5. 将 [text](#anchor) 转换为 @ref anchor "text"

CONTENT=$(cat "$1")
FILENAME=$(basename "$1")

# 使用 perl 方便处理跨行
echo -e "$CONTENT" | perl -0777 -pe 's/==([^=]*)==/\@mark{$1}/g; s/\*\*(.*?)\*\*/<b>$1<\/b>/g; s/(?<!\*)\*([^\*\n]+?)\*(?!\*)/<i>$1<\/i>/g; s/<!-- id:(\S+) -->\s*\n\s*(#+)\s*(.*)/$2 $3 {#$1}/g; s/\[([^\]]+)\]\(\.\.\/[^\)]+\/([^\/\)]+)\)/\@ref $2 "$1"/g; s/\[([^\]]+)\]\(#([^ \)]+)\)/\@ref $2 "$1"/g; s/\[([^\]]+)\]\(\.\/1_基础.md\)/\@ref basics_doc "$1"/g'
