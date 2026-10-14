Working directory: /app

The file `/app/data/numbers.txt` contains one integer per line (may be positive, zero, or negative, with duplicates).

Create a Python script `/app/solve.py` that:

1. Reads every line of `/app/data/numbers.txt`, strips whitespace, and skips empty lines.
2. Parses each remaining line as a base-10 integer.
3. Drops all negative values and all duplicate values (keep only the first occurrence of each value).
4. Writes the resulting sum as a plain decimal integer (no extra text, no thousands separators, followed by a newline) to `/app/result.txt`.

Run the script so that `/app/result.txt` exists when you finish. Do not modify `/app/data/numbers.txt`.
