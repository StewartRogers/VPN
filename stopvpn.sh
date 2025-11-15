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
# VARIABLES
#
SSERVICE="q"
TEMP_DEST=""  # Will be set to SOURCE_DIR during file processing

# Function to stop services

shutdown_services() {
    echo ""
    if [[ "$SSERVICE" == "q" ]]; then
        # Shutdown qbittorrent
        SERVICE="qbittorrent-nox"
        echo "... Stopping qbittorrent"
        if pgrep -x "$SERVICE" >/dev/null; then
            echo "... $SERVICE is running"
            sudo pkill -f "$SERVICE"
            echo "... $SERVICE has been stopped."
        else
            echo "... $SERVICE is not running"
        fi
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
    echo "... Stopping OpenVPN Server"
    SERVICE="openvpn"
    if pgrep -x "$SERVICE" >/dev/null; then
        echo "... $SERVICE is running"
        sudo pkill -f "$SERVICE"
        echo "... $SERVICE has been stopped."
    else
        echo "... $SERVICE is not running"
    fi
    sleep 1

    echo ""
    echo "... Stopping checkip script"
    SERVICE="checkip.sh"
    if pgrep -x "$SERVICE" >/dev/null; then
        echo "... $SERVICE is running"
        sudo pkill -f "$SERVICE"
        echo "... $SERVICE has been stopped."
    else
        echo "... $SERVICE is not running"
    fi
    sleep 1
    echo ""
    screen -S "checkip" -p 0 -X quit > /dev/null
}

 # Main script logic
if [[ "$1" == "--shutdown-only" ]]; then
    echo "Running shutdown commands only..."
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
    current_file=1

    echo -e "\nFound $total_files video files to process."

    # Process each file one at a time
    for file in "${files[@]}"; do
        [ -e "$file" ] || continue

        # Get relative path for display
        rel_path="${file#./}"
        
        echo -e "\n----------------------------------------"
        echo "File $current_file of $total_files"
        echo "----------------------------------------"
        echo "Current file: $rel_path"

        # Note if file is in subdirectory for later
        in_subdir=false
        if [[ "$file" =~ / ]]; then
            in_subdir=true
            dir_path=$(dirname "$file")
            echo "Note: This file is in subdirectory: $dir_path"
        fi

        # Ask if user wants to rename this file
        read -rp "Do you want to rename this file? [y/N/q=quit]: " rename_file
        
        # Check for quit command
        if [[ "${rename_file,,}" == "q" ]]; then
            echo "Exiting file processing..."
            break
        fi
        
        # Process rename if requested
        if [[ "$rename_file" =~ ^[Yy]$ ]]; then
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
              sed -E 's/\s+1080p.*$//i; s/\s+720p.*$//i; s/\s+480p.*$//i; s/\s+2160p.*$//i' | \
              sed -E 's/\s+x264.*$//i; s/\s+x265.*$//i; s/\s+h\.?264.*$//i' | \
              sed -E 's/\s+(BluRay|WEB-DL|WEB|AMZN|FLUX|YIFY|RARBG).*$//i' | \
              sed -E 's/\[.*\]//g; s/\(.*\)//g' | \
              sed -E 's/\s+$//; s/^\s+//')
            
            # Format the cleaned filename by converting spaces to dots
            newname="$(echo "$cleanname" | sed -E 's/\s+/./g')".${ext,,}

            # Show the proposed new name
            echo -e "\nProposed rename:"
            echo "  From: $file"
            echo "  To:   $newname"

            # Confirm the rename
            read -rp "Proceed with this rename? [y/N]: " confirm_rename

            if [[ "$confirm_rename" =~ ^[Yy]$ ]]; then
                if [[ -e "$newname" ]]; then
                    echo "  File already exists: $newname"
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
                else
                    if mv -n "$file" "$newname" 2>/dev/null; then
                        echo "  Renamed successfully."
                    else
                        echo "  Error: Failed to rename file."
                        continue
                    fi

                    # If file was in subdirectory, ask about moving it to main directory first
                    if [[ "$in_subdir" == "true" ]]; then
                        read -rp "Move renamed file to main directory? [y/N/q=quit]: " move_to_main
                        
                        if [[ "${move_to_main,,}" == "q" ]]; then
                            echo "Exiting file processing..."
                            break
                        fi
                        
                        if [[ "$move_to_main" =~ ^[Yy]$ ]]; then
                            current_dir=$(dirname "$file")
                            if [[ -e "$newname" ]]; then
                                echo "Note: File with same name exists in main directory"
                                read -rp "Overwrite file in main directory? [y/N]: " overwrite_main
                                if [[ "$overwrite_main" =~ ^[Yy]$ ]]; then
                                    if mv -f "$current_dir/$newname" "$newname" 2>/dev/null; then
                                        file="$newname"
                                        echo "Moved to main directory (overwritten)"
                                    else
                                        echo "Error: Failed to move file to main directory"
                                    fi
                                else
                                    file="$current_dir/$newname"
                                    echo "Keeping file in subdirectory"
                                fi
                            else
                                if mv "$current_dir/$newname" "$newname" 2>/dev/null; then
                                    file="$newname"
                                    echo "Moved to main directory"
                                else
                                    echo "Error: Failed to move file to main directory"
                                fi
                            fi
                        fi
                    fi

                    # Ask about moving to destination folder
                    read -rp "Move file to destination folder? [y/N]: " confirm_move
                    if [[ "$confirm_move" =~ ^[Yy]$ ]]; then
                        # Check if file exists in destination
                        if [[ -e "$TEMP_DEST/$newname" ]]; then
                            echo "  Warning: File already exists in destination: $TEMP_DEST/$newname"
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
                        else
                            if mv -n "$file" "$TEMP_DEST/" 2>/dev/null; then
                                echo "  Moved to destination folder."
                            else
                                echo "  Error: Failed to move file to destination."
                            fi
                        fi
                    else
                        echo "  Keeping file in current location."
                    fi
                fi
            else
                echo "Skipping rename and move operations for this file."
            fi

        echo -e "\nFile processing completed."
        
        # Increment file counter
        ((current_file++))
        
        # Ask to continue to next file if not the last one
        if [[ $current_file -le $total_files ]]; then
            echo -e "\n----------------------------------------"
            read -rp "Continue to next file? [Y/n/q=quit]: " continue_processing
            if [[ "${continue_processing,,}" == "q" ]]; then
                echo "Exiting file processing..."
                break
            elif [[ "${continue_processing,,}" == "n" ]]; then
                echo "Stopping file processing..."
                break
            fi
        fi
    done

    # Return to original directory
    cd - > /dev/null || { echo "Failed to return to original directory"; exit 1; }
    
    echo -e "\n========================================="
    echo "File processing completed."
    echo "Processed $current_file of $total_files files."
    echo "========================================="
    echo ""
    exit 0
else
    echo -e "\nSkipped renaming and moving files.\n"
fi

#
# END
#