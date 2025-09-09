#!/bin/bash

# Constants
WINTER_SOLSTICE=355
DAYS_IN_YEAR=365
DEFAULT_YEAR=2024  # Use a leap year for better accuracy

# Prompt user
read -p "Enter a date (e.g., 6 March, 9th September, 09/09/25): " raw_input

# 🧹 Remove ordinal suffixes: st, nd, rd, th (case-insensitive)
cleaned_input=$(echo "$raw_input" | sed -E 's/\<([0-9]{1,2})(st|nd|rd|th)\>/\1/gI')

# 📅 Check for numeric UK-style format: DD/MM/YY or DD-MM-YY
if [[ "$cleaned_input" =~ ^([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{2,4})$ ]]; then
    day=${BASH_REMATCH[1]}
    month=${BASH_REMATCH[2]}
    year=${BASH_REMATCH[3]}
    
    # Expand 2-digit year to 4-digit
    if [ ${#year} -eq 2 ]; then
        if [ "$year" -ge 70 ]; then
            year="19$year"
        else
            year="20$year"
        fi
    fi

    # Format: YYYY-MM-DD
    parsed_date=$(date -d "$year-$month-$day" +"%Y-%m-%d" 2>/dev/null)
else
    # Handle natural language input (e.g. "6 March", "March 6")
    parsed_date=$(date -d "$cleaned_input $DEFAULT_YEAR" +"%Y-%m-%d" 2>/dev/null)
fi

# ❌ If parsing failed
if [ -z "$parsed_date" ]; then
    echo "❌ Invalid date input. Please try formats like:"
    echo "   '6 March', '9th September', '09/09/25', '9-9-25'"
    exit 1
fi

# 🔢 Get day-of-year from parsed date
day_of_year=$(date -d "$parsed_date" "+%j")

# 🔁 Calculate opposite day
offset=$((day_of_year - WINTER_SOLSTICE))
opposite_day=$((WINTER_SOLSTICE - offset))

# ⏱️ Wrap around the year if needed
if [ "$opposite_day" -lt 1 ]; then
    opposite_day=$((opposite_day + DAYS_IN_YEAR))
elif [ "$opposite_day" -gt $DAYS_IN_YEAR ]; then
    opposite_day=$((opposite_day - DAYS_IN_YEAR))
fi

# 🗓️ Convert both day numbers to readable dates
input_date_fmt=$(date -d "$parsed_date" +"%d %B")
opposite_date_fmt=$(date -d "$DEFAULT_YEAR-01-01 +$((opposite_day - 1)) days" +"%d %B")

# 📤 Output
echo ""
echo "📅 You entered:     $input_date_fmt (Day $day_of_year)"
echo "🔄 Opposite date:   $opposite_date_fmt (Day $opposite_day)"
