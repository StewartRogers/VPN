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
LOG_DIR="${LOG_DIR:-$HOME/.vpn_logs}"

#
# Logging function
#
log_message() {
    local level=$1
    local message=$2
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $message" | tee -a "$LOG_DIR/vpn.log" 2>/dev/null
}

#
# VARIABLES
#
SSERVICE="q"
TEMP_DEST=""  # Will be set to SOURCE_DIR during file processing

#
# Cleanup security measures
#
cleanup_killswitch() {
    if [ -f "$BACKUP_DIR/iptables.backup" ]; then
        echo "... Restoring original iptables rules"
        log_message "INFO" "Restoring original iptables rules"
        sudo iptables-restore < "$BACKUP_DIR/iptables.backup"
        rm "$BACKUP_DIR/iptables.backup"
        echo "... iptables rules restored"
    fi
}

cleanup_dns() {
    if [ -f "$BACKUP_DIR/resolv.conf.backup" ]; then
        echo "... Restoring original DNS configuration"
        log_message "INFO" "Restoring original DNS configuration"
        sudo chattr -i /etc/resolv.conf 2>/dev/null || true
        sudo mv "$BACKUP_DIR/resolv.conf.backup" /etc/resolv.conf
        echo "... DNS configuration restored"
    fi
}

restore_ipv6() {
    if [ -f "$BACKUP_DIR/ipv6_all.backup" ] && [ -f "$BACKUP_DIR/ipv6_default.backup" ]; then
        echo "... Restoring IPv6 settings"
        log_message "INFO" "Restoring IPv6 settings"
        ORIGINAL_ALL=$(cat "$BACKUP_DIR/ipv6_all.backup")
        ORIGINAL_DEFAULT=$(cat "$BACKUP_DIR/ipv6_default.backup")
        
        # Validate values are 0 or 1
        if [[ "$ORIGINAL_ALL" =~ ^[01]$ ]] && [[ "$ORIGINAL_DEFAULT" =~ ^[01]$ ]]; then
            sudo sysctl -w net.ipv6.conf.all.disable_ipv6=$ORIGINAL_ALL > /dev/null
            sudo sysctl -w net.ipv6.conf.default.disable_ipv6=$ORIGINAL_DEFAULT > /dev/null
            rm "$BACKUP_DIR/ipv6_all.backup" "$BACKUP_DIR/ipv6_default.backup"
            echo "... IPv6 settings restored"
        else
            echo "... Warning: Invalid IPv6 backup values, skipping restoration"
            log_message "WARN" "Invalid IPv6 backup values: all=$ORIGINAL_ALL, default=$ORIGINAL_DEFAULT"
        fi
    elif [ -f "$BACKUP_DIR/ipv6.backup" ]; then
        # Fallback for old backup format
        echo "... Restoring IPv6 settings (legacy format)"
        log_message "INFO" "Restoring IPv6 settings (legacy format)"
        ORIGINAL=$(cat "$BACKUP_DIR/ipv6.backup")
        
        # Validate value is 0 or 1
        if [[ "$ORIGINAL" =~ ^[01]$ ]]; then
            sudo sysctl -w net.ipv6.conf.all.disable_ipv6=$ORIGINAL > /dev/null
            sudo sysctl -w net.ipv6.conf.default.disable_ipv6=$ORIGINAL > /dev/null
            rm "$BACKUP_DIR/ipv6.backup"
            echo "... IPv6 settings restored"
        else
            echo "... Warning: Invalid IPv6 backup value, skipping restoration"
            log_message "WARN" "Invalid IPv6 backup value: $ORIGINAL"
        fi
    fi
}

restore_qbittorrent_config() {
    local CONFIG_FILE="$HOME/.config/qBittorrent/qBittorrent.conf"
    if [ -f "$BACKUP_DIR/qBittorrent.conf.backup" ]; then
        echo "... Restoring qBittorrent configuration"
        log_message "INFO" "Restoring qBittorrent configuration"
        mv "$BACKUP_DIR/qBittorrent.conf.backup" "$CONFIG_FILE"
        echo "... qBittorrent configuration restored"
    fi
}

cleanup_ufw_rule() {
    if [ -f "$BACKUP_DIR/ufw_rule.backup" ]; then
        echo "... Removing UFW rule added by VPN startup"
        log_message "INFO" "Removing UFW rule"
        UFW_RULE=$(cat "$BACKUP_DIR/ufw_rule.backup")
        
        # Validate the rule format (port/protocol) and port range
        if [[ "$UFW_RULE" =~ ^([0-9]+)/(tcp|udp)$ ]]; then
            local PORT="${BASH_REMATCH[1]}"
            if [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; then
                sudo ufw delete allow "$UFW_RULE" > /dev/null 2>&1
                echo "... UFW rule removed"
            else
                echo "... Warning: Invalid UFW port number in backup, skipping removal"
                log_message "WARN" "Invalid UFW port number: $UFW_RULE"
            fi
        else
            echo "... Warning: Invalid UFW rule format in backup, skipping removal"
            log_message "WARN" "Invalid UFW rule format: $UFW_RULE"
        fi
        rm "$BACKUP_DIR/ufw_rule.backup"
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
            echo "... Stopping $service (PID: $pid)"
            log_message "INFO" "Stopping $service (PID: $pid)"
            kill $pid 2>/dev/null
            sleep 1
            # Force kill if still running
            if kill -0 $pid 2>/dev/null; then
                kill -9 $pid 2>/dev/null
            fi
            rm "$pid_file"
            echo "... $service has been stopped"
        else
            echo "... $service not running (stale PID file)"
            rm "$pid_file"
        fi
    else
        # Fallback to pkill if no PID file
        if pgrep -f "$service" >/dev/null; then
            echo "... Stopping $service (no PID file, using pkill)"
            log_message "WARN" "Stopping $service using pkill (no PID file found)"
            sudo pkill -f "$service"
            echo "... $service has been stopped"
        else
            echo "... $service is not running"
        fi
    fi
}

# Function to stop services

shutdown_services() {
    echo ""
    log_message "INFO" "Starting service shutdown"
    
    if [[ "$SSERVICE" == "q" ]]; then
        # Shutdown qbittorrent using PID
        echo "... Stopping qbittorrent"
        stop_service_by_pid "qbittorrent"
        sleep 1
    else
        # Shutdown Deluge Web and Deluge Daemon
        for SERVICE in deluge-web deluged; do
            if [[ "$SERVICE" == "deluge-web" ]]; then
                echo "... Stopping Deluge Web Server"
            else
                echo "... Stopping Deluge Server"
            fi
            if pgrep -x "$SERVICE" >/dev/null; then
                echo "... $SERVICE is running"
                if [[ "$SERVICE" == "deluged" ]]; then
                    xDELUGE="$(deluge-console "connect 127.0.0.1:58846 ; pause * ; halt ; quit")"
                    echo "... ${xDELUGE}"
                fi
                sudo pkill -f "$SERVICE"
                echo "... $SERVICE has been stopped."
            else
                echo "... $SERVICE is not running"
            fi
            sleep 1
        done
    fi

    echo ""
    echo "... Stopping checkip script"
    stop_service_by_pid "checkip"
    sleep 1
    screen -S "checkip" -p 0 -X quit > /dev/null 2>&1

    echo ""
    echo "... Stopping OpenVPN"
    if pgrep -f "openvpn" >/dev/null; then
        log_message "INFO" "Stopping OpenVPN"
        sudo pkill -f "openvpn"
        echo "... OpenVPN has been stopped"
        sleep 2
    else
        echo "... OpenVPN is not running"
    fi

    # Clean up all security measures
    echo ""
    echo "... Cleaning up security measures"
    log_message "INFO" "Cleaning up security measures"
    cleanup_killswitch
    cleanup_dns
    restore_ipv6
    restore_qbittorrent_config
    cleanup_ufw_rule
    echo "... All security measures reversed"
    log_message "INFO" "All security measures reversed - system restored to normal"
    echo ""
}

 # Main script logic
if [[ "$1" == "--shutdown-only" ]]; then
    echo "Running shutdown commands only..."
    log_message "INFO" "Shutdown requested (--shutdown-only)"
    shutdown_services
    echo -e "\nDone."
    exit 0
fi

# Prompt user to shutdown services
read -rp "Do you want to shutdown services? [y/N]: " do_shutdown
if [[ "${do_shutdown,,}" == "y" ]]; then
    shutdown_services
else
    echo -e "\nSkipped shutting down services.\n"
fi

# Prompt user to skip rename and move files section
read -rp "Do you want to rename and move video files? [y/N]: " do_rename
if [[ "${do_rename,,}" == "y" ]]; then
    read -er -p "Enter the full path to the source directory: " SOURCE_DIR
    SOURCE_DIR="${SOURCE_DIR%/}"
    # Set destination to same directory as source
    TEMP_DEST="$SOURCE_DIR"

    if [[ ! -d "$SOURCE_DIR" ]]; then
        echo "Error: Source directory not found: $SOURCE_DIR"
        exit 1
    fi

    # Count all matching files (root + subdirectories)
    file_count=$(find "$SOURCE_DIR" -type f \( -iname "*.mp4" -o -iname "*.mkv" \) | wc -l)
    if [[ "$file_count" -eq 0 ]]; then
        echo "No .mp4 or .mkv files found in $SOURCE_DIR."
        echo ""
        exit 0
    fi

    # Work from the source directory so we can use relative paths
    cd "$SOURCE_DIR" || { echo "Failed to access $SOURCE_DIR"; exit 1; }

    # Get all video files (including those in subdirectories)
    readarray -t files < <(find . -type f \( -iname "*.mp4" -o -iname "*.mkv" \))
    total_files=${#files[@]}

    if [[ $total_files -eq 0 ]]; then
        echo "No video files found."
        exit 0
    fi

    # Sort files by directory depth (subdirectories first)
    IFS=$'\n' files=($(printf "%s\n" "${files[@]}" | sort))
    total_files=${#files[@]}

    echo -e "\nFound $total_files video files to process."

    # Process each file one at a time
    for file in "${files[@]}"; do
        [ -e "$file" ] || continue

        # Get relative path for display
        rel_path="${file#./}"
        
        echo -e "\n----------------------------------------"
        echo "Current file: $rel_path"

        # Note if file is in subdirectory for later
        in_subdir=false
        if [[ "$file" =~ ^\.\/[^/]+\/ ]]; then
            # File path has at least one subdirectory level after ./
            in_subdir=true
            dir_path=$(dirname "$file")
            echo "Note: This file is in subdirectory: $dir_path"
        fi
        
        # Extract filename and extension
        filename=$(basename "$file")
        ext="${file##*.}"
        
        # Clean up filename using multiple sed operations:
        # 1. Remove file extensions
        # 2. Remove content in brackets and parentheses
        # 3. Remove quality tags, release group names, and encoding info
        # 4. Trim trailing/leading spaces
        cleanname=$(echo "$filename" | \
          sed -E 's/\.[mM][kK][vV]$//; s/\.[mM][pP]4$//' | \
          sed -E 's/[\s.]+1080p.*$//i; s/[\s.]+720p.*$//i; s/[\s.]+480p.*$//i; s/[\s.]+2160p.*$//i' | \
          sed -E 's/[\s.]+(HEVC|x264|x265|H\.?264|h\.?264).*$//i' | \
          sed -E 's/[\s.]+(BluRay|BLU-RAY|WEBRip|WEB-DL|WEB|AMZN|FLUX|YIFY|RARBG|MeGusta).*$//i' | \
          sed -E 's/[\s.]+(10bit|8bit|AAC|DDP|FLAC).*$//i' | \
          sed -E 's/\[.*\]//g; s/\(.*\)//g' | \
          sed -E 's/[\s.]+$//; s/^[\s.]+//' | \
          sed -E 's/\./ /g')
        
        # Format the cleaned filename by converting spaces to dots
        newname="$(echo "$cleanname" | sed -E 's/\s+/./g')".${ext,,}

        # Show the proposed new name
        echo -e "\nProposed rename:"
        echo "  From: $file"
        echo "  To:   $newname"

        # Confirm the rename
        read -rp "Proceed with this rename? [y/N/q=quit]: " confirm_rename

        if [[ "${confirm_rename,,}" == "q" ]]; then
            echo "Exiting file processing..."
            break
        fi

        if [[ "$confirm_rename" =~ ^[Yy]$ ]]; then
                if [[ -e "$newname" ]]; then
                    # Check if it's the same file (same size)
                    current_size=$(stat -c%s "$file" 2>/dev/null)
                    existing_size=$(stat -c%s "$newname" 2>/dev/null)
                    
                    if [[ "$current_size" == "$existing_size" ]]; then
                        # Same file already exists - skip rename
                        echo "  File with same name and size already exists: $newname"
                        echo "  Skipping rename operation."
                        file="$newname"  # Update file reference to the renamed version
                    else
                        # Different file with same name - ask to overwrite
                        echo "  File already exists: $newname (different size)"
                        read -rp "  Do you want to overwrite the existing file? [y/N]: " confirm_overwrite
                        if [[ "$confirm_overwrite" =~ ^[Yy]$ ]]; then
                            if mv -f "$file" "$newname" 2>/dev/null; then
                                echo "  File overwritten successfully."
                            else
                                echo "  Error: Failed to overwrite file."
                                continue
                            fi
                        else
                            echo "  Skipping: keeping original file."
                            continue
                        fi
                    fi
                else
                    # For subdirectory files, rename within the subdirectory
                    # For main directory files, rename in place
                    if [[ "$in_subdir" == "true" ]]; then
                        current_dir=$(dirname "$file")
                        subdir_newname="$current_dir/$newname"
                        if mv -n "$file" "$subdir_newname" 2>/dev/null; then
                            echo "  Renamed successfully."
                            file="$subdir_newname"
                        else
                            echo "  Error: Failed to rename file."
                            continue
                        fi
                    else
                        if mv -n "$file" "$newname" 2>/dev/null; then
                            echo "  Renamed successfully."
                            file="$newname"
                        else
                            echo "  Error: Failed to rename file."
                            continue
                        fi
                    fi

                    # If file was in subdirectory, ask about moving it to main directory first
                    if [[ "$in_subdir" == "true" ]]; then
                        read -rp "Move renamed file to main directory? [y/N/q=quit]: " move_to_main
                        
                        if [[ "${move_to_main,,}" == "q" ]]; then
                            echo "Exiting file processing..."
                            break
                        fi
                        
                        if [[ "$move_to_main" =~ ^[Yy]$ ]]; then
                            if [[ -e "$newname" ]]; then
                                # Check if it's the same file (same size)
                                subdir_size=$(stat -c%s "$file" 2>/dev/null)
                                main_size=$(stat -c%s "$newname" 2>/dev/null)
                                
                                if [[ "$subdir_size" == "$main_size" ]]; then
                                    # Same file already exists in main - skip move
                                    echo "Note: File with same name and size already exists in main directory"
                                    echo "Skipping move operation."
                                else
                                    # Different file with same name - ask to overwrite
                                    echo "Note: File with same name exists in main directory (different size)"
                                    read -rp "Overwrite file in main directory? [y/N]: " overwrite_main
                                    if [[ "$overwrite_main" =~ ^[Yy]$ ]]; then
                                        if mv -f "$file" "$newname" 2>/dev/null; then
                                            file="$newname"
                                            echo "Moved to main directory (overwritten)"
                                            # Remove the subdirectory if the move was successful
                                            subdir_path=$(dirname "$file")
                                            if [[ -d "$subdir_path" ]]; then
                                                rm -rf "$subdir_path" 2>/dev/null
                                                echo "Removed subdirectory: $subdir_path"
                                            fi
                                        else
                                            echo "Error: Failed to move file to main directory"
                                        fi
                                    else
                                        echo "Keeping file in subdirectory"
                                    fi
                                fi
                            else
                                if mv "$file" "$newname" 2>/dev/null; then
                                    file="$newname"
                                    echo "Moved to main directory"
                                    # Remove the subdirectory if the move was successful
                                    subdir_path=$(dirname "$file")
                                    if [[ -d "$subdir_path" ]]; then
                                        rm -rf "$subdir_path" 2>/dev/null
                                        echo "Removed subdirectory: $subdir_path"
                                    fi
                                else
                                    echo "Error: Failed to move file to main directory"
                                fi
                            fi
                        else
                            echo "Keeping file in subdirectory"
                        fi
                    else
                        # File is already in main directory, no need to move
                        file="$newname"
                    fi

                    # Only ask about moving to destination folder if it's different from source
                    if [[ "$TEMP_DEST" != "." && "$TEMP_DEST" != "$SOURCE_DIR" ]]; then
                        # Check if file exists in destination with same name and size
                        if [[ -e "$TEMP_DEST/$newname" ]]; then
                            current_size=$(stat -c%s "$file" 2>/dev/null)
                            dest_size=$(stat -c%s "$TEMP_DEST/$newname" 2>/dev/null)
                            
                            if [[ "$current_size" == "$dest_size" ]]; then
                                # File with same name and size exists - skip silently
                                echo "  Skipping move: file with same name and size already exists in destination."
                            else
                                # File exists but different size - ask about overwrite
                                echo "  Warning: File already exists in destination with different size: $TEMP_DEST/$newname"
                                read -rp "  Do you want to overwrite the existing file? [y/N]: " confirm_move_overwrite
                                if [[ "$confirm_move_overwrite" =~ ^[Yy]$ ]]; then
                                    if mv -f "$file" "$TEMP_DEST/" 2>/dev/null; then
                                        echo "  Moved to destination (overwritten)."
                                    else
                                        echo "  Error: Failed to move file to destination."
                                    fi
                                else
                                    echo "  Skipping move: keeping file in current location."
                                fi
                            fi
                        else
                            read -rp "Move file to destination folder? [y/N]: " confirm_move
                            if [[ "$confirm_move" =~ ^[Yy]$ ]]; then
                                if mv -n "$file" "$TEMP_DEST/" 2>/dev/null; then
                                    echo "  Moved to destination folder."
                                else
                                    echo "  Error: Failed to move file to destination."
                                fi
                            else
                                echo "  Keeping file in current location."
                            fi
                        fi
                    fi
                fi
            else
                echo "Skipping rename and move operations for this file."
            fi
        
        # Ask to continue to next file
        echo -e "\n----------------------------------------"
        read -rp "Continue to next file? [Y/n/q=quit]: " continue_processing
        if [[ "${continue_processing,,}" == "q" ]]; then
            echo "Exiting file processing..."
            break
        elif [[ "${continue_processing,,}" == "n" ]]; then
            echo "Stopping file processing..."
            break
        fi
    done

    # Return to original directory
    cd - > /dev/null || { echo "Failed to return to original directory"; exit 1; }
    
    echo -e "\n========================================="
    echo "File processing completed."
    echo "========================================="
    echo ""
    exit 0
else
    echo -e "\nSkipped renaming and moving files.\n"
fi

#
# END
#