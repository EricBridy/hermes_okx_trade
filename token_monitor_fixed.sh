#!/bin/bash
# 轻量级token监测脚本
# 使用最小资源检查token使用情况

# 获取当前配置
CONFIG_PATH="$HOME/.hermes/config.yaml"
if [ -f "$CONFIG_PATH" ]; then
    # 使用yq或awk正确提取YAML中的模型设置
    CURRENT_MODEL=$(awk '/default:/ {print $2}' "$CONFIG_PATH" | tr -d '[:space:]')
    echo "当前模型: $CURRENT_MODEL"
else
    CURRENT_MODEL="glm-4.6v"
    echo "配置文件不存在，使用默认模型: $CURRENT_MODEL"
fi

# 模拟token使用情况检查（实际应用中需要真实检查）
# 这里使用一个简单的随机值模拟，实际应用中应该调用API
TOKEN_USAGE=$((RANDOM % 30))  # 生成0-29的随机数模拟token使用率

echo "当前token使用率: ${TOKEN_USAGE}%"

# 检查是否需要切换
if [ "$TOKEN_USAGE" -lt 20 ]; then
    echo "警告: Token使用率低于20%，准备切换模型"
    
    # 模型切换顺序
    if [ "$CURRENT_MODEL" = "glm-4.6v" ]; then
        NEW_MODEL="glm-4.7"
    elif [ "$CURRENT_MODEL" = "glm-4.7" ]; then
        NEW_MODEL="glm-4.6v"  # 循环回到第一个
    else
        NEW_MODEL="glm-4.6v"  # 默认回到第一个
    fi
    
    echo "切换模型: $CURRENT_MODEL → $NEW_MODEL"
    
    # 更新配置（轻量级操作）
    if [ -f "$CONFIG_PATH" ]; then
        # 使用正确的YAML格式进行替换
        sed -i "s/default: $CURRENT_MODEL/default: $NEW_MODEL/" "$CONFIG_PATH"
        echo "配置已更新"
    else
        echo "配置文件不存在，无法更新"
    fi
else
    echo "Token使用率正常，无需切换"
fi

# 确保脚本执行时间极短，不消耗token
exit 0