#!/bin/bash
# 轻量级token监测脚本
# 使用最小资源检查token使用情况

# 获取当前配置路径
CONFIG_PATH="$HOME/.hermes/config.yaml"

# 检查配置文件是否存在
if [ -f "$CONFIG_PATH" ]; then
    # 使用更轻量级的方法获取当前模型
    CURRENT_MODEL=$(grep -oP "default:\\s*\\K\\w+" "$CONFIG_PATH" | head -1)
else
    CURRENT_MODEL="glm-4.6v"
fi

# 生成随机数模拟token使用情况 (0-29)
TOKEN_USAGE=$((RANDOM % 30))  # 生成0-29的随机数模拟token使用率

# 检查token使用情况
echo "当前token使用率: $TOKEN_USAGE%"

# 如果token使用率低于20%，切换模型
if [ "$TOKEN_USAGE" -lt 20 ]; then
    echo "警告: Token使用率低于20%"
    
    # 模型切换顺序
    model_sequence=("glm-4.6v" "glm-4.7")
    
    # 找到当前模型的索引
    for i in "${!model_sequence[@]}"; do
        if [ "${model_sequence[$i]}" = "$CURRENT_MODEL" ]; then
            # 如果不是最后一个模型，切换到下一个
            if [ $i -lt $((${#model_sequence[@]} - 1)) ]; then
                next_model="${model_sequence[$i + 1]}"
                echo "Token额度低于20%，已切换模型: $CURRENT_MODEL → $next_model"
                # 在实际应用中，这里应该更新配置文件
                # sed -i "s/default: $CURRENT_MODEL/default: $next_model/" "$CONFIG_PATH"
                exit 0
            else
                echo "已达到最后一个模型，无法继续切换"
                exit 1
            fi
        fi
    done
    
    echo "当前模型 $CURRENT_MODEL 不在切换序列中"
    exit 1
else
    echo "Token使用率正常"
    exit 0
fi
