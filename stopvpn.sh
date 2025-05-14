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
SHOME="/home/pi/MyPiFiles/vpn/"
SSERVICE="q"

# Function to stop services
shutdown_services() {
    echo ""
    if [[ "$SSERVICE" == "q" ]]; then
        echo "... Stopping qbittorrent"
        SERVICE="qbittorrent-nox"
    else
        echo "... Stopping Deluge Web Server"
        SERVICE="deluge-web"
    fi

    if pgrep -x "$SERVICE" >/dev/null; then
        echo "... $SERVICE is running"
        sudo killall "$SERVICE"
        echo "... $SERVICE has been stopped."
        sleep 1
    else
        echo "... $SERVICE is not running"
    fi
    sleep 1

    if [[ "$SSERVICE" == "q" ]]; then
        SERVICE="qbittorrent-nox"
    else
        echo ""
        echo "... Stopping Deluge Server"
        SERVICE="deluged"
        if pgrep -x "$SERVICE" >/dev/null; then
            echo "... $SERVICE is running"
            echo ""
            xDELUGE="$(deluge-console "connect 127.0.0.1:58846 ; pause * ; halt ; quit")"
            echo "... ${xDELUGE}"
            sleep 1
        else
            echo "... $SERVICE is not running"
        fi
    fi

    echo ""
    echo "... Stopping OpenVPN Server"
    SERVICE="openvpn"
    if pgrep -x "$SERVICE" >/dev/null; then
        echo "... $SERVICE is running"
        sudo killall "$SERVICE"
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
        sudo killall "$SERVICE"
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

# Full script logic
shutdown_services

# Rename and move files
read -rp "Enter the full path to the source directory: " SOURCE_DIR
SOURCE_DIR="${SOURCE_DIR%/}"
TEMP_DEST="$SOURCE_DIR"

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Error: Source directory not found: $SOURCE_DIR"
    exit 1
fi

file_count=$(find "$SOURCE_DIR" -mindepth 2 -type f \( -iname "*.mp4" -o -iname "*.mkv" \) | wc -l)
if [[ "$file_count" -eq 0 ]]; then
    echo "No .mp4 or .mkv files found in subdirectories of $SOURCE_DIR."
    echo ""
    exit 0
fi

mkdir -p "$TEMP_DEST"
cd "$SOURCE_DIR" || { echo "Failed to access $SOURCE_DIR"; exit 1; }
find . -mindepth 2 -type f \( -iname "*.mp4" -o -iname "*.mkv" \) -exec mv -n "{}" . \;

for file in *.mp4 *.mkv; do
    [ -e "$file" ] || continue
    filename=$(basename "$file")
    cleanname=$(echo "$filename" | sed -E 's/\.[0-9]{3,4}p.*//; s/\[[^]]*\]//g; s/\([^)]*\)//g; s/\.[mM][kK][vV]$//; s/\.[mM][pP]4$//' | sed -E 's/[._]+/ /g' | sed -E 's/ +$//; s/^ +//')
    ext="${file##*.}"
    newname="$(echo "$cleanname" | tr ' ' '.')".${ext,,}

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
            else
                echo "  Left in $SOURCE_DIR."
            fi
        fi
    else
        echo "  Skipped."
    fi
done

echo -e "\n\nDone.\n"

#
# END
#