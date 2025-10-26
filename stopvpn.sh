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

#
# Function to stop services
#
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

#
# Main script logic
#
if [[ "$1" == "--shutdown-only" ]]; then
    echo "Running shutdown commands only..."
    shutdown_services
    echo -e "\nDone."
    exit 0
fi

#
# Prompt user to shutdown services
#
read -rp "Do you want to shutdown services? [y/N]: " do_shutdown
if [[ "${do_shutdown,,}" == "y" ]]; then
    shutdown_services
else
    echo -e "\nSkipped shutting down services.\n"
fi

#############################################################################
# File Processing Section
# This section handles the organization and cleanup of video files:
# 1. Prompts for source directory
# 2. Processes files from subdirectories (optional)
# 3. Renames files to a clean format
# 4. Moves files to a destination folder
# Each operation requires explicit user confirmation for safety
#############################################################################

# Initial prompt to run the file processing section
read -rp "Do you want to rename, move and convert (if necessary) video files? [y/N]: " do_rename
if [[ "${do_rename,,}" == "y" ]]; then
    # Get source directory with tab completion enabled (-e flag)
    read -er -p "Enter the full path to the source directory: " SOURCE_DIR
    # Remove trailing slash if present
    SOURCE_DIR="${SOURCE_DIR%/}"
    # Set destination to same directory (can be modified for different destination)
    TEMP_DEST="$SOURCE_DIR"

    # Validate source directory exists
    if [[ ! -d "$SOURCE_DIR" ]]; then
        echo "Error: Source directory not found: $SOURCE_DIR"
        exit 1
    fi

    # Count number of video files (MP4 and MKV) in directory and subdirectories
    file_count=$(find "$SOURCE_DIR" -type f \( -iname "*.mp4" -o -iname "*.mkv" \) | wc -l)
    # Exit if no video files found
    if [[ "$file_count" -eq 0 ]]; then
        echo "No .mp4 or .mkv files found in $SOURCE_DIR or its subdirectories."
        echo ""
        exit 0
    fi

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
            # 4. Convert dots and underscores to spaces
            # 5. Trim leading/trailing spaces
            cleanname=$(echo "$filename" | \
              sed -E 's/\.[mM][kK][vV]$//; s/\.[mM][pP]4$//' | \
              sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g' | \
              sed -E 's/\.1080p\.(.*$)//' | \
              sed -E 's/\.720p\.(.*$)//' | \
              sed -E 's/\.480p\.(.*$)//' | \
              sed -E 's/\.2160p\.(.*$)//' | \
              sed -E 's/(\.BluRay\.|\.WEB-DL\.|\.WEB\.|\.[Xx]264\.|\.AAC.*|\.AMZN\.|\.DDP.*|\.H264\.|\.FLUX\.|\.YIFY\.|\.RARBG\.|\.EZTVx\.to\.)(.*$)//' | \
              sed -E 's/ +$//; s/^ +//')
            
            # Format the cleaned filename
            newname="$(echo "$cleanname" | tr -s ' ' | sed -E 's/ /./g; s/\.+/./g')".${ext,,}

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
                    mv -f "$file" "$newname"
                    echo "  File overwritten successfully."
                else
                    echo "  Skipping: keeping original file."
                    moved_file="$file"
                    continue
                fi
            else
                mv -n "$file" "$newname"
                echo "  Renamed successfully."

                # If file was in subdirectory, ask about moving it to main directory first
                if [[ "$in_subdir" == "true" ]]; then
                    read -rp "Move renamed file to main directory? [y/N/q=quit]: " move_to_main
                    
                    if [[ "${move_to_main,,}" == "q" ]]; then
                        echo "Exiting file processing..."
                        break
                    fi
                    
                    if [[ "$move_to_main" =~ ^[Yy]$ ]]; then
                        dir_path=$(dirname "$file")
                        if [[ -e "./$newname" ]]; then
                            echo "Note: File with same name exists in main directory"
                            read -rp "Overwrite file in main directory? [y/N]: " overwrite_main
                            if [[ "$overwrite_main" =~ ^[Yy]$ ]]; then
                                mv -f "$dir_path/$newname" "./"
                                file="./$newname"
                                echo "Moved to main directory (overwritten)"
                            else
                                file="$dir_path/$newname"
                                echo "Keeping file in subdirectory"
                            fi
                        else
                            mv "$dir_path/$newname" "./"
                            file="./$newname"
                            echo "Moved to main directory"
                        fi
                    fi
                fi

                # Ask about moving to destination folder
                read -rp "Move file to destination folder ($TEMP_DEST)? [y/N]: " confirm_move
                if [[ "$confirm_move" =~ ^[Yy]$ ]]; then
                    # Check if file exists in destination
                    if [[ -e "$TEMP_DEST/$newname" ]]; then
                        echo "  Warning: File already exists in destination: $TEMP_DEST/$newname"
                        read -rp "  Do you want to overwrite the existing file? [y/N]: " confirm_move_overwrite
                        if [[ "$confirm_move_overwrite" =~ ^[Yy]$ ]]; then
                            mv -f "$file" "$TEMP_DEST/"
                            echo "  Moved to $TEMP_DEST (overwritten)."
                        else
                            echo "  Skipping move: keeping file in current location."
                            moved_file="$file"
                        fi
                    else
                        mv -n "$file" "$TEMP_DEST/"
                        echo "  Moved to $TEMP_DEST"
                    fi
                    moved_file="$TEMP_DEST/$newname"
                else
                    echo "  Left in $SOURCE_DIR"
                    moved_file="$newname"
                fi
                fi
            else
                echo "Skipping move operation"
                moved_file="$newname"
            fi
        else
            echo "Skipping rename operation"
            # Don't offer move since we didn't rename
            moved_file="$file"
        fi

        echo -e "\nFile processing completed successfully"
        
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
    
    echo -e "\nDone processing files.\n"
    exit 0
else
    echo -e "\nSkipped renaming and moving files.\n"
    exit 0
fi

#
# END
#