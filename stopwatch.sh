#!/usr/bin/env bash

# Stopwatch script - press Ctrl+C to stop and show final time

# Format seconds into HH:MM:SS
format_time() {
    local total=$1
    local hours=$((total / 3600))
    local minutes=$(((total % 3600) / 60))
    local seconds=$((total % 60))
    printf "%02d:%02d:%02d" "$hours" "$minutes" "$seconds"
}

# Cleanup on exit (Ctrl+C)
finish() {
    local final_elapsed=$(($(date +%s) - start_time))
    printf "\nStopwatch stopped.\nTotal elapsed time: %s\n" "$(format_time $final_elapsed)"
    exit 0
}

trap finish INT

# Start timing
start_time=$(date +%s)
echo "Stopwatch running. Press Ctrl+C to stop."

# Main loop – update display every second
while true; do
    current_time=$(date +%s)
    elapsed=$((current_time - start_time))
    printf "\rElapsed time: %s" "$(format_time $elapsed)"
    sleep 1
done