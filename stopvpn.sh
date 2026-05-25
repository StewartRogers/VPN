#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# Original Author: Stewart Rogers
# This licensed under the MIT License
# A short and simple permissive license with conditions only requiring
# preservation of copyright and license notices. Licensed works, modifications,
# and larger works may be distributed under different terms and without source code.
#

#
# Load configuration file if exists
#
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

# Set default values if not in config
BACKUP_DIR="${BACKUP_DIR:-/tmp/vpn_backups}"
PID_DIR="${PID_DIR:-/tmp/vpn_pids}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/vpn_logs}"

# Ensure required directories exist
mkdir -p "$BACKUP_DIR" "$PID_DIR" "$LOG_DIR"

#
# Logging function
#
log_message() {
    local level=$1
    local message=$2
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $message" >> "$LOG_DIR/vpn.log" 2>/dev/null
}

divider() {
    echo "------------------------------------------------------------"
}

rotate_logs() {
    if [ -f "$LOG_DIR/vpn.log" ]; then
        local size=$(stat -c%s "$LOG_DIR/vpn.log" 2>/dev/null || echo 0)
        if [ $size -gt 10485760 ]; then  # 10MB
            mv "$LOG_DIR/vpn.log" "$LOG_DIR/vpn.log.1"
            [ -f "$LOG_DIR/vpn.log.1.gz" ] && rm "$LOG_DIR/vpn.log.1.gz"
            gzip "$LOG_DIR/vpn.log.1" 2>/dev/null || true
            log_message "INFO" "Log rotated (previous file > 10MB)"
        fi
    fi
}

#
# VARIABLES
#
SSERVICE="q"
TEMP_DEST=""  # Will be set to SOURCE_DIR during file processing

reset_ufw() {
    echo "  Resetting UFW to base state..."
    log_message "INFO" "Resetting UFW to base state..."
    if sudo bash "$SCRIPT_DIR/ufw_base.sh" >> "$LOG_DIR/vpn.log" 2>&1; then
        echo "  UFW base state restored - outgoing unrestricted"
        log_message "INFO" "UFW base state restored - outgoing unrestricted"
    else
        echo "  WARNING: UFW reset failed - run manually: sudo bash $SCRIPT_DIR/ufw_base.sh"
        log_message "WARN" "UFW reset failed - run manually: sudo bash $SCRIPT_DIR/ufw_base.sh"
    fi
}

#
# PID-based process stopping
#
stop_service_by_pid() {
    local service=$1
    local pid_file="$PID_DIR/${service}.pid"

    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 $pid 2>/dev/null; then
            echo "  Stopping $service (PID: $pid)"
            log_message "INFO" "Stopping $service (PID: $pid)"
            kill $pid 2>/dev/null
            sleep 1
            if kill -0 $pid 2>/dev/null; then
                kill -9 $pid 2>/dev/null
            fi
            rm "$pid_file"
        else
            echo "  $service not running (stale PID file removed)"
            log_message "INFO" "$service not running (stale PID file removed)"
            rm "$pid_file"
        fi
    else
        if pgrep -f "$service" >/dev/null; then
            echo "  Stopping $service (pkill fallback)"
            log_message "WARN" "Stopping $service using pkill (no PID file found)"
            sudo pkill -f "$service"
        else
            echo "  $service is not running"
            log_message "INFO" "$service is not running"
        fi
    fi
}

# Function to stop services

shutdown_services() {
    rotate_logs
    log_message "INFO" "Starting service shutdown"
    divider
    echo "  Shutting down services..."
    echo ""

    if [[ "$SSERVICE" == "q" ]]; then
        echo "  [ qBittorrent ]"
        stop_service_by_pid "qbittorrent"
        sleep 1
    else
        echo "  [ Deluge ]"
        for SERVICE in deluge-web deluged; do
            if [[ "$SERVICE" == "deluge-web" ]]; then
                echo "  Stopping Deluge Web Server"
                log_message "INFO" "Stopping Deluge Web Server"
            else
                echo "  Stopping Deluge Daemon"
                log_message "INFO" "Stopping Deluge Server"
            fi
            if pgrep -x "$SERVICE" >/dev/null; then
                if [[ "$SERVICE" == "deluged" ]]; then
                    xDELUGE="$(deluge-console "connect 127.0.0.1:58846 ; pause * ; halt ; quit")"
                    log_message "INFO" "$xDELUGE"
                fi
                sudo pkill -f "$SERVICE"
            fi
            sleep 1
        done
    fi

    echo ""
    echo "  [ Monitoring ]"
    stop_service_by_pid "checkip"
    sleep 1
    screen -S "checkip" -p 0 -X quit > /dev/null 2>&1
    log_message "INFO" "Stopping checkip script"

    echo ""
    echo "  [ OpenVPN ]"
    if pgrep -x "openvpn" >/dev/null; then
        echo "  Stopping OpenVPN"
        log_message "INFO" "Stopping OpenVPN"
        sudo pkill -x "openvpn"
        sleep 2
    else
        echo "  OpenVPN is not running"
        log_message "INFO" "OpenVPN is not running"
    fi

    echo ""
    echo "  [ Firewall ]"
    reset_ufw

    divider
    echo ""
}

# Main script logic
if [[ "$1" == "--shutdown-only" ]]; then
    log_message "INFO" "Shutdown requested (--shutdown-only)"
    shutdown_services
    echo "Done."
    exit 0
fi

# Prompt user to shutdown services
read -rp "Shutdown services? [y/N]: " do_shutdown
do_shutdown=$(echo "$do_shutdown" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
if [[ "$do_shutdown" == "y" ]]; then
    shutdown_services
else
    echo ""
    echo "  Skipped service shutdown."
    echo ""
fi

#
# Helper functions for file processing
#
clean_filename() {
    local filename=$1
    local ext=$2
    
    # Remove extensions, quality tags, release info, brackets/parens, convert spaces to dots
    local cleanname=$(echo "$filename" | \
      sed -E 's/\.[mM][kK][vV]$//; s/\.[mM][pP]4$//' | \
      sed -E 's/[\s.]+[0-9]{3,4}p.*$//i' | \
      sed -E 's/[\s.]+(HEVC|x26[45]|H\.?264|h\.?264|BluRay|BLU-RAY|WEBRip|WEB-DL|WEB|AMZN|FLUX|YIFY|RARBG|MeGusta|10bit|8bit|AAC|DDP|FLAC).*$//i' | \
      sed -E 's/\[.*\]//g; s/\(.*\)//g' | \
      sed -E 's/[\s.]+$//; s/^[\s.]+//' | \
      sed -E 's/\./ /g' | \
      sed -E 's/\s+/./g')
    
    echo "${cleanname}.${ext,,}"
}

file_exists_same_size() {
    local file1=$1
    local file2=$2
    
    if [[ ! -e "$file2" ]]; then
        return 1
    fi
    
    local size1=$(stat -c%s "$file1" 2>/dev/null)
    local size2=$(stat -c%s "$file2" 2>/dev/null)
    
    [[ "$size1" == "$size2" ]]
}

rename_video_file() {
    local src=$1
    local dst=$2
    local force=${3:-false}
    
    if file_exists_same_size "$src" "$dst"; then
        log_message "INFO" "File already exists with same size: $dst"
        echo "  Already exists (same size) - skipping"
        return 2  # Skip code
    fi
    
    local mv_flag="-n"
    [[ "$force" == "true" ]] && mv_flag="-f"
    
    if mv $mv_flag "$src" "$dst" 2>/dev/null; then
        log_message "INFO" "Renamed: $src -> $dst"
        echo "  ✓ Renamed successfully"
        return 0
    else
        log_message "ERROR" "Failed to rename: $src -> $dst"
        echo "  ✗ Failed to rename"
        return 1
    fi
}

# Prompt user to skip rename and move files section
read -rp "Rename and move video files? [y/N]: " do_rename
do_rename=$(echo "$do_rename" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
if [[ "$do_rename" == "y" ]]; then
    log_message "INFO" "Starting video file processing"
    read -er -p "Enter the full path to the source directory: " SOURCE_DIR
    SOURCE_DIR="${SOURCE_DIR%/}"

    if [[ ! -d "$SOURCE_DIR" ]]; then
        log_message "ERROR" "Source directory not found: $SOURCE_DIR"
        echo "Error: Source directory not found: $SOURCE_DIR"
        exit 1
    fi

    # Count matching files
    file_count=$(find "$SOURCE_DIR" -type f \( -iname "*.mp4" -o -iname "*.mkv" \) | wc -l)
    log_message "INFO" "Found $file_count video files in $SOURCE_DIR"
    
    if [[ "$file_count" -eq 0 ]]; then
        echo "No .mp4 or .mkv files found in $SOURCE_DIR."
        echo ""
        exit 0
    fi

    cd "$SOURCE_DIR" || { log_message "ERROR" "Failed to access $SOURCE_DIR"; exit 1; }

    # Get and sort files
    readarray -t files < <(find . -type f \( -iname "*.mp4" -o -iname "*.mkv" \) | sort)
    echo -e "\nFound ${#files[@]} video files to process.\n"

    # Process each file
    for file in "${files[@]}"; do
        [ -e "$file" ] || continue

        rel_path="${file#./}"
        echo -e "----------------------------------------"
        echo "File: $rel_path"
        
        # Check if in subdirectory
        in_subdir=false
        subdir_path=""
        if [[ "$file" =~ ^\.\/[^/]+\/ ]]; then
            in_subdir=true
            subdir_path=$(dirname "$file")
            echo "  Location: subdirectory ($subdir_path)"
        fi
        
        # Generate clean filename
        filename=$(basename "$file")
        ext="${file##*.}"
        newname=$(clean_filename "$filename" "$ext")
        
        echo -e "\n  Proposed: $newname"
        read -rp "  Rename? [y/N/q]: " confirm_rename

        case "${confirm_rename,,}" in
            q)
                log_message "INFO" "File processing aborted by user"
                echo "Exiting..."
                break
                ;;
            y)
                # Handle renaming based on location
                if [[ "$in_subdir" == "true" ]]; then
                    # Rename in subdirectory
                    local_newname="$subdir_path/$newname"
                    if rename_video_file "$file" "$local_newname"; then
                        file="$local_newname"
                        
                        # Ask to flatten to main directory
                        read -rp "  Move to main directory? [Y/n]: " move_main
                        if [[ ! "${move_main,,}" == "n" ]]; then
                            if file_exists_same_size "$file" "$newname"; then
                                echo "  Already exists in main (same size) - skipping"
                                log_message "INFO" "Skipped move to main: $newname (duplicate)"
                            elif rename_video_file "$file" "$newname"; then
                                # Clean up empty subdirectory
                                rmdir "$subdir_path" 2>/dev/null && echo "  Removed empty subdirectory"
                            fi
                        fi
                    fi
                else
                    # Rename in place
                    rename_video_file "$file" "$newname"
                fi
                ;;
            *)
                echo "  Skipped"
                log_message "INFO" "Skipped: $rel_path"
                ;;
        esac
        
        # Continue prompt
        read -rp "\nContinue? [Y/n/q]: " continue_next
        case "${continue_next,,}" in
            q|n)
                log_message "INFO" "File processing stopped by user"
                echo "Stopping..."
                break
                ;;
        esac
    done

    cd - > /dev/null
    log_message "INFO" "File processing completed"
    echo -e "\n========================================="
    echo "File processing completed."
    echo "=========================================\n"
    exit 0
else
    echo -e "\nSkipped renaming and moving files.\n"
    log_message "INFO" "File processing skipped by user"
fi

#
# END
#