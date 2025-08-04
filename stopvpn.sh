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

# Prompt user to skip rename, move and convert files section
read -rp "Do you want to rename, move and convert (if necessary) video files? [y/N]: " do_rename
if [[ "${do_rename,,}" == "y" ]]; then
    read -rp "Enter the full path to the source directory: " SOURCE_DIR
    SOURCE_DIR="${SOURCE_DIR%/}"
    TEMP_DEST="$SOURCE_DIR"

    if [[ ! -d "$SOURCE_DIR" ]]; then
        echo "Error: Source directory not found: $SOURCE_DIR"
        exit 1
    fi

    file_count=$(find "$SOURCE_DIR" -type f \( -iname "*.mp4" -o -iname "*.mkv" \) | wc -l)
    if [[ "$file_count" -eq 0 ]]; then
        echo "No .mp4 or .mkv files found in $SOURCE_DIR or its subdirectories."
        echo ""
        exit 0
    fi

    mkdir -p "$TEMP_DEST"
    cd "$SOURCE_DIR" || { echo "Failed to access $SOURCE_DIR"; exit 1; }
    find . -mindepth 2 -type f \( -iname "*.mp4" -o -iname "*.mkv" \) -exec mv -n "{}" . \;


    for file in *.mp4 *.mkv; do
        [ -e "$file" ] || continue
        filename=$(basename "$file")
        ext="${file##*.}"
        cleanname=$(echo "$filename" | \
          sed -E 's/\.[mM][kK][vV]$//; s/\.[mM][pP]4$//' | \
          sed -E 's/\[[^]]*\]//g; s/\([^)]*\)//g' | \
          sed -E 's/(1080p|720p|480p|2160p|AMZN|WEB[-_. ]?DL|BluRay|DDP[0-9.]+|H[ ._-]?264|x264|AAC|FLUX|DD5[.1]?|DDP5[.1]?|DDP|EVO|YIFY|RARBG|EZTVx.to|WEB|DL|DDP|H|264)[^ ]*.*$//' | \
          sed -E 's/[._]+/ /g' | \
          sed -E 's/ +$//; s/^ +//')
        # Collapse multiple spaces to one, then convert to dot-separated, then remove duplicate dots
        newname="$(echo "$cleanname" | tr -s ' ' | sed -E 's/ /./g; s/\.+/./g')".${ext,,}

        echo ""
        echo "Rename file:"
        echo "  From: $file"
        echo "  To:   $newname"
        read -rp "Proceed with rename? [y/N]: " confirm_rename

        if [[ "$confirm_rename" =~ ^[Yy]$ ]]; then
            if [[ -e "$newname" ]]; then
                echo "  Skipping: $newname already exists."
            else
                mv -n "$file" "$newname"
                echo "  Renamed successfully."

                read -rp "Move $newname to temporary folder ($TEMP_DEST)? [y/N]: " confirm_move
                if [[ "$confirm_move" =~ ^[Yy]$ ]]; then
                    mv -n "$newname" "$TEMP_DEST/"
                    echo "  Moved to $TEMP_DEST."
                    moved_file="$TEMP_DEST/$newname"
                else
                    echo "  Left in $SOURCE_DIR."
                    moved_file="$newname"
                fi

                # If MKV, offer to convert to MP4 in the destination folder
                if [[ "${ext,,}" == "mkv" ]]; then
                    read -rp "Convert $moved_file to MP4 using ffmpeg? [y/N]: " confirm_convert
                    if [[ "$confirm_convert" =~ ^[Yy]$ ]]; then
                        mp4name="${moved_file%.*}.mp4"
                        echo "  Converting $moved_file to $mp4name ..."
                        if ffmpeg -i "$moved_file" -map 0 -c:v libx264 -preset slow -crf 22 -c:a aac -b:a 192k -movflags +faststart -c:s mov_text "$mp4name"; then
                            echo "  Conversion successful. Removing original MKV."
                            rm -f "$moved_file"
                        else
                            echo "  Conversion failed. Keeping original MKV."
                        fi
                    fi
                fi
            fi
        else
            echo "  Skipped."
        fi
    done

    echo -e "\n\nDone.\n"
else
    echo -e "\nSkipped renaming and moving files.\n"
fi

#
# END
#