#!/bin/bash
SESSION_NAME="pixav"

# Check if session already exists
tmux has-session -t $SESSION_NAME 2>/dev/null
if [ $? == 0 ]; then
  echo "Tmux session '$SESSION_NAME' already exists. Use 'tmux attach -t $SESSION_NAME' to view it."
  exit 0
fi

echo "Starting pixAV python workers in tmux session '$SESSION_NAME'..."

# Step 1: Ensure docker infrastructure is running
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Step 2: Create new session for Python workers in detached mode
tmux new-session -d -s $SESSION_NAME -n strm_resolver 'uv run uvicorn pixav.strm_resolver.app:create_app --factory --host 0.0.0.0 --port 8000'

# Step 3: Add other background workers as windows
tmux new-window -t $SESSION_NAME -n sht_probe 'uv run python -m pixav.sht_probe.worker'
tmux new-window -t $SESSION_NAME -n media_loader 'uv run python -m pixav.media_loader.worker'
tmux new-window -t $SESSION_NAME -n maxwell_core 'uv run python -m pixav.maxwell_core.worker'

echo "All workers started."
echo "Use 'tmux attach -t $SESSION_NAME' to view their outputs."
echo "Windows available:"
echo " 0: strm_resolver"
echo " 1: sht_probe"
echo " 2: media_loader"
echo " 3: maxwell_core"
