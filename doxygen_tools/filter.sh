#!/bin/bash
#
# Copyright (C) 2026 Aaron <communicate_aaron@outlook.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
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
