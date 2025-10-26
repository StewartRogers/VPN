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

    # Process subdirectories if requested
    read -rp "Do you want to move files from subdirectories to the main directory? [y/N]: " process_subdirs
    if [[ "$process_subdirs" =~ ^[Yy]$ ]]; then
        # Find and display files that would be moved
        echo -e "\nFiles that will be moved from subdirectories:"
        subdir_files=$(find . -mindepth 2 -type f \( -iname "*.mp4" -o -iname "*.mkv" \) -print)
        
        if [[ -z "$subdir_files" ]]; then
            echo "No files found in subdirectories."
        else
            echo "$subdir_files"
            read -rp "Proceed with moving these files to the main directory? [y/N]: " confirm_submove
            
            if [[ "$confirm_submove" =~ ^[Yy]$ ]]; then
                # Move files with error checking
                if find . -mindepth 2 -type f \( -iname "*.mp4" -o -iname "*.mkv" \) -exec mv -n "{}" . \; ; then
                    echo "Files moved successfully from subdirectories."
                else
                    echo "Error: Some files could not be moved."
                    exit 1
                fi
            else
                echo "Skipped moving files from subdirectories."
            fi
        fi
    fi

    # Get list of files to process
    files=(*.mp4 *.mkv)
    total_files=${#files[@]}
    current_file=1

    # Process files in the current directory one at a time
    for file in "${files[@]}"; do
        [ -e "$file" ] || continue

        echo -e "\n----------------------------------------"
        echo "File $current_file of $total_files"
        echo "----------------------------------------"
        echo "Current file: $file"

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
              sed -E 's/(1080p|720p|480p|2160p|AMZN|WEB[-_. ]?DL|BluRay|DDP[0-9.]+|H[ ._-]?264|x264|AAC|FLUX|DD5[.1]?|DDP5[.1]?|DDP|EVO|YIFY|RARBG|EZTVx.to|WEB|DL|DDP|H|264)[^ ]*.*$//' | \
              sed -E 's/[._]+/ /g' | \
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

                # Prompt for moving the renamed file
                read -rp "Move $newname to destination folder ($TEMP_DEST)? [y/N]: " confirm_move
                if [[ "$confirm_move" =~ ^[Yy]$ ]]; then
                    # Check if file already exists in destination
                    if [[ -e "$TEMP_DEST/$newname" ]]; then
                        echo "  Warning: File already exists in destination: $TEMP_DEST/$newname"
                        read -rp "  Do you want to overwrite the existing file? [y/N]: " confirm_move_overwrite
                        if [[ "$confirm_move_overwrite" =~ ^[Yy]$ ]]; then
                            mv -f "$newname" "$TEMP_DEST/"
                            echo "  Moved to $TEMP_DEST (overwritten)."
                        else
                            echo "  Skipping move: keeping file in current location."
                            moved_file="$newname"
                        fi
                    else
                        mv -n "$newname" "$TEMP_DEST/"
                        echo "  Moved to $TEMP_DEST"
                    fi
                    moved_file="$TEMP_DEST/$newname"
                else
                    echo "  Left in $SOURCE_DIR"
                    moved_file="$newname"
                fi
            fi
        else
            echo "Skipping file move operation"
            moved_file="$newname"
        fi

        echo -e "\nFile processing completed successfully"
        else
            echo "Skipping rename operation"
            # Don't offer move since we didn't rename
        fi
        
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