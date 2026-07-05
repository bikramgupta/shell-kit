#!/bin/bash
input=$(cat)

# Extract data
MODEL=$(echo "$input" | jq -r '.model.display_name')
CONTEXT_SIZE=$(echo "$input" | jq -r '.context_window.context_window_size')
USAGE=$(echo "$input" | jq '.context_window.current_usage')
COST=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')
CURRENT_DIR=$(echo "$input" | jq -r '.workspace.current_dir')

# Format a token count: raw number under 1000 (bash integer division would
# otherwise truncate it to "0K"), one-decimal K above that.
fmt_tokens() {
    local n=$1
    if [ "$n" -lt 1000 ]; then
        echo "$n"
    else
        awk -v n="$n" 'BEGIN { printf "%.1fK", n / 1000 }'
    fi
}

if [ "$USAGE" != "null" ]; then
    # input/output here are for the last API turn only, not the session total
    INPUT_TOKENS=$(echo "$USAGE" | jq -r '.input_tokens')
    OUTPUT_TOKENS=$(echo "$USAGE" | jq -r '.output_tokens')
    CACHE_READ=$(echo "$USAGE" | jq -r '.cache_read_input_tokens')

    # Total current context
    CURRENT_TOKENS=$(echo "$USAGE" | jq '.input_tokens + .cache_creation_input_tokens + .cache_read_input_tokens')
    PERCENT_USED=$((CURRENT_TOKENS * 100 / CONTEXT_SIZE))

    # Format in K for readability
    CURRENT_K=$((CURRENT_TOKENS / 1000))
    CONTEXT_K=$((CONTEXT_SIZE / 1000))
    CACHE_READ_K=$((CACHE_READ / 1000))
    IN_FMT=$(fmt_tokens "$INPUT_TOKENS")
    OUT_FMT=$(fmt_tokens "$OUTPUT_TOKENS")

    # Truncate directory path
    DIR=$(basename "$CURRENT_DIR")

    # Build statusline with useful info
    echo "[$MODEL] ${CURRENT_K}K/${CONTEXT_K}K (${PERCENT_USED}%) | ${CACHE_READ_K}K cached | ${IN_FMT} in | ${OUT_FMT} out | \$${COST}"
else
    DIR=$(basename "$CURRENT_DIR")
    echo "[$MODEL] Context: 0% | $DIR"
fi
