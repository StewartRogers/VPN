#!/bin/bash
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

# Prompt user to skip rename and move files section
read -rp "Do you want to rename and move video files? [y/N]: " do_rename
if [[ "${do_rename,,}" == "y" ]]; then
    read -rp "Enter the full path to the source directory: " SOURCE_DIR
    SOURCE_DIR="${SOURCE_DIR%/}"

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

    # Iterate over every matching file (including subdirectories)
    while IFS= read -r -d '' file; do
        # file is like ./subdir/movie.mp4 or ./movie.mp4
        rel=${file#./}
        [ -n "$rel" ] || rel="$file"

        # Ask whether to process this file
        echo ""
        read -rp "Process file '$rel'? [y/N]: " process_file
        if [[ "${process_file,,}" != "y" ]]; then
            echo "  Skipping $rel"
            continue
        fi

        filename=$(basename "$rel")
        # Clean the filename (same rules as before)
        cleanname=$(echo "$filename" | sed -E 's/\.[0-9]{3,4}p.*//; s/\[[^]]*\]//g; s/\([^)]*\)//g; s/\.[mM][kK][vV]$//; s/\.[mM][pP]4$//' | sed -E 's/[._]+/ /g' | sed -E 's/ +$//; s/^ +//')
        ext="${filename##*.}"
        newname="$(echo "$cleanname" | tr ' ' '.')".${ext,,}

        echo "  From: $rel"
        echo "  To:   $newname"
        read -rp "  Confirm rename? [y/N]: " confirm_rename
        if [[ "${confirm_rename,,}" != "y" ]]; then
            echo "  Rename skipped for $rel"
            continue
        fi

        # If the file is in a subdirectory (rel contains a slash), move it to the SOURCE_DIR when renaming
        if [[ "$rel" == */* ]]; then
            # Ensure target does not already exist
            if [[ -e "$newname" ]]; then
                echo "  Skipping: $newname already exists in target directory."
                continue
            fi
            mv -n -- "$rel" "$newname"
            echo "  Moved and renamed to $newname"
        else
            # File already in root SOURCE_DIR
            if [[ "$filename" == "$newname" ]]; then
                echo "  Name unchanged, skipping."
                continue
            fi
            if [[ -e "$newname" ]]; then
                echo "  Skipping: $newname already exists."
                continue
            fi
            mv -n -- "$rel" "$newname"
            echo "  Renamed to $newname"
        fi

    done < <(find . -type f \( -iname "*.mp4" -o -iname "*.mkv" \) -print0)

    echo -e "\n\nDone.\n"
else
    echo -e "\nSkipped renaming and moving files.\n"
fi

#
# END
#