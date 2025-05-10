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

#
# Stopping torrent server. Change SSERVICE variable from q to use Deluge
#
echo ""
if [[ "$SSERVICE" == "q" ]];
  then
     echo "... Stopping qbittorrent"
     SERVICE="qbittorrent-nox"
  else
     echo "... Stopping Deluge Web Server"
     SERVICE="deluge-web"
fi

if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    sudo killall $SERVICE
    echo "... $SERVICE has been stopped."
    sleep 1
else
    echo "... $SERVICE is not running"
fi
sleep 1

#
# Stopping Deluge console server. Change SSERVICE variable from q to use Deluge
#
if [[ "$SSERVICE" == "q" ]];
  then
     SERVICE="qbittorrent-nox"
  else
     echo ""
     echo "... Stopping Deluge Server"
     SERVICE="deluged"
     if pgrep -x "$SERVICE" >/dev/null
     then
         echo "... $SERVICE is running"
         echo ""
         xDELUGE="$(deluge-console "connect 127.0.0.1:58846 ; pause * ; halt ; quit")"
         echo "... ${xDELUGE}"
         sleep 1
     else
         echo "... $SERVICE is not running"
     fi
fi

#
# Stopping OpenVPN server.
#
echo ""
echo "... Stopping OpenVPN Server"
SERVICE="openvpn"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    sudo killall $SERVICE
    echo "... $SERVICE has been stopped."
else
    echo "... $SERVICE is not running"
fi
sleep 1

#
# Stopping checkip script server.
#
echo ""
echo "... Stopping checkip script"
SERVICE="checkip.sh"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    sudo killall $SERVICE
    echo "... $SERVICE has been stopped."
else
    echo "... $SERVICE is not running"
fi
sleep 1
echo ""
screen -S "checkip" -p 0 -X quit > /dev/null

#
# Rename and move files
#

# Prompt for the source directory
read -rp "Enter the full path to the source directory: " SOURCE_DIR

# Trim trailing slashes
SOURCE_DIR="${SOURCE_DIR%/}"

# Set temp destination to same as source
TEMP_DEST="$SOURCE_DIR"

# Check if source directory exists
if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Error: Source directory not found: $SOURCE_DIR"
    exit 1
fi

# Check if any .mp4 or .mkv files exist in subdirectories
file_count=$(find "$SOURCE_DIR" -mindepth 2 -type f \( -iname "*.mp4" -o -iname "*.mkv" \) | wc -l)

if [[ "$file_count" -eq 0 ]]; then
    echo "No .mp4 or .mkv files found in subdirectories of $SOURCE_DIR."
    echo ""
    exit 0
fi

# Ensure the temp destination exists
mkdir -p "$TEMP_DEST"

# Change to the source directory
cd "$SOURCE_DIR" || { echo "Failed to access $SOURCE_DIR"; exit 1; }

# Move .mp4 and .mkv files from subdirectories to current directory
find . -mindepth 2 -type f \( -iname "*.mp4" -o -iname "*.mkv" \) -exec mv -n "{}" . \;

# Process files in current directory
for file in *.mp4 *.mkv; do
    [ -e "$file" ] || continue

    filename=$(basename "$file")

    # Clean filename
    cleanname=$(echo "$filename" | \
        sed -E 's/\.[0-9]{3,4}p.*//; s/\[[^]]*\]//g; s/\([^)]*\)//g; s/\.[mM][kK][vV]$//; s/\.[mM][pP]4$//' | \
        sed -E 's/[._]+/ /g' | \
        sed -E 's/ +$//; s/^ +//')

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

echo -e "\nDone."


#
# END
#
