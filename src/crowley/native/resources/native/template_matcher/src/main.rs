use std::io::{self, Read};

const MAGIC: &[u8; 8] = b"CRWTM001";
const HEADER_LEN: usize = 48;
const SEARCH: u32 = 1;
const SCORE_AT: u32 = 2;
const MAX_INPUT_BYTES: usize = 64 * 1024 * 1024;
const MAX_FRAME_DIM: u32 = 4096;
const MAX_TEMPLATE_DIM: u32 = 512;
const MAX_COMPARISON_BYTES: u64 = 1u64 << 31;

struct Request<'a> {
    operation: u32,
    frame_width: usize,
    template_width: usize,
    template_height: usize,
    region_x: usize,
    region_y: usize,
    region_width: usize,
    region_height: usize,
    frame: &'a [u8],
    template: &'a [u8],
}

fn read_u32(bytes: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes([
        bytes[offset],
        bytes[offset + 1],
        bytes[offset + 2],
        bytes[offset + 3],
    ])
}

fn checked_product(left: u64, right: u64, name: &str) -> Result<u64, String> {
    left.checked_mul(right)
        .ok_or_else(|| format!("{name} size overflows"))
}

fn parse_request(data: &[u8]) -> Result<Request<'_>, String> {
    if data.len() > MAX_INPUT_BYTES {
        return Err("input exceeds 64 MiB".to_string());
    }
    if data.len() < HEADER_LEN {
        return Err("input is shorter than the 48-byte header".to_string());
    }
    if &data[..8] != MAGIC {
        return Err("protocol magic is invalid".to_string());
    }

    let operation = read_u32(data, 8);
    if operation != SEARCH && operation != SCORE_AT {
        return Err("operation is unknown".to_string());
    }
    if read_u32(data, 12) != 0 {
        return Err("reserved header field is nonzero".to_string());
    }

    let frame_width_raw = read_u32(data, 16);
    let frame_height_raw = read_u32(data, 20);
    let template_width_raw = read_u32(data, 24);
    let template_height_raw = read_u32(data, 28);
    let region_x_raw = read_u32(data, 32);
    let region_y_raw = read_u32(data, 36);
    let region_width_raw = read_u32(data, 40);
    let region_height_raw = read_u32(data, 44);

    if frame_width_raw == 0
        || frame_height_raw == 0
        || frame_width_raw > MAX_FRAME_DIM
        || frame_height_raw > MAX_FRAME_DIM
    {
        return Err("frame dimensions are outside the fixed bounds".to_string());
    }
    if template_width_raw == 0
        || template_height_raw == 0
        || template_width_raw > MAX_TEMPLATE_DIM
        || template_height_raw > MAX_TEMPLATE_DIM
    {
        return Err("template dimensions are outside the fixed bounds".to_string());
    }
    if region_width_raw == 0 || region_height_raw == 0 {
        return Err("search region dimensions must be positive".to_string());
    }

    let frame_width = frame_width_raw as usize;
    let template_width = template_width_raw as usize;
    let template_height = template_height_raw as usize;
    let region_x = region_x_raw as usize;
    let region_y = region_y_raw as usize;
    let region_width = region_width_raw as usize;
    let region_height = region_height_raw as usize;

    let region_right = (region_x_raw as u64)
        .checked_add(region_width_raw as u64)
        .ok_or_else(|| "search region horizontal coordinate overflows".to_string())?;
    let region_bottom = (region_y_raw as u64)
        .checked_add(region_height_raw as u64)
        .ok_or_else(|| "search region vertical coordinate overflows".to_string())?;
    if region_right > frame_width_raw as u64 || region_bottom > frame_height_raw as u64 {
        return Err("search region is outside the frame".to_string());
    }
    if operation == SCORE_AT && (region_width != template_width || region_height != template_height)
    {
        return Err("SCORE_AT region must equal template dimensions".to_string());
    }
    if template_width > region_width || template_height > region_height {
        return Err("template is larger than the search region".to_string());
    }

    let frame_pixels = checked_product(frame_width_raw as u64, frame_height_raw as u64, "frame")?;
    let frame_bytes = checked_product(frame_pixels, 3, "frame byte")?;
    let template_pixels = checked_product(
        template_width_raw as u64,
        template_height_raw as u64,
        "template",
    )?;
    let template_bytes = checked_product(template_pixels, 3, "template byte")?;
    let candidate_columns = (region_width - template_width + 1) as u64;
    let candidate_rows = (region_height - template_height + 1) as u64;
    let candidate_count = checked_product(candidate_columns, candidate_rows, "candidate")?;
    let comparison_bytes = if operation == SCORE_AT {
        template_bytes
    } else {
        checked_product(candidate_count, template_bytes, "comparison")?
    };
    if comparison_bytes > MAX_COMPARISON_BYTES {
        return Err("comparison byte count exceeds 2^31".to_string());
    }

    let expected_len = (HEADER_LEN as u64)
        .checked_add(frame_bytes)
        .and_then(|size| size.checked_add(template_bytes))
        .ok_or_else(|| "input size overflows".to_string())?;
    if expected_len > MAX_INPUT_BYTES as u64 {
        return Err("input exceeds 64 MiB".to_string());
    }
    let expected_len = expected_len as usize;
    if data.len() < expected_len {
        return Err("input is truncated".to_string());
    }
    if data.len() > expected_len {
        return Err("input has trailing bytes".to_string());
    }

    let frame_end = HEADER_LEN + frame_bytes as usize;
    Ok(Request {
        operation,
        frame_width,
        template_width,
        template_height,
        region_x,
        region_y,
        region_width,
        region_height,
        frame: &data[HEADER_LEN..frame_end],
        template: &data[frame_end..expected_len],
    })
}

fn candidate_sad(request: &Request<'_>, x: usize, y: usize) -> u64 {
    let mut sad = 0u64;
    let row_bytes = request.template_width * 3;
    for row in 0..request.template_height {
        let frame_start = ((y + row) * request.frame_width + x) * 3;
        let template_start = row * row_bytes;
        for channel in 0..row_bytes {
            let left = request.frame[frame_start + channel];
            let right = request.template[template_start + channel];
            sad += left.abs_diff(right) as u64;
        }
    }
    sad
}

fn rectangles_intersect(
    left_x: usize,
    left_y: usize,
    right_x: usize,
    right_y: usize,
    width: usize,
    height: usize,
) -> bool {
    left_x < right_x + width
        && right_x < left_x + width
        && left_y < right_y + height
        && right_y < left_y + height
}

fn json_result(
    status: &str,
    sad: u64,
    count: usize,
    x: usize,
    y: usize,
    width: usize,
    height: usize,
) {
    println!(
        "{{\"status\":\"{status}\",\"sad\":{sad},\"count\":{count},\"box\":[{x},{y},{width},{height}]}}"
    );
}

fn execute(request: &Request<'_>) {
    let count = request.template.len();
    let threshold = 5 * count as u64;
    let max_x = request.region_x + request.region_width - request.template_width;
    let max_y = request.region_y + request.region_height - request.template_height;

    if request.operation == SCORE_AT {
        let x = request.region_x;
        let y = request.region_y;
        let sad = candidate_sad(request, x, y);
        let status = if sad <= threshold {
            "matched"
        } else {
            "rejected"
        };
        json_result(
            status,
            sad,
            count,
            x,
            y,
            request.template_width,
            request.template_height,
        );
        return;
    }

    let mut best: Option<(u64, usize, usize)> = None;
    let mut accepted: Vec<(u64, usize, usize)> = Vec::new();
    for y in request.region_y..=max_y {
        for x in request.region_x..=max_x {
            let sad = candidate_sad(request, x, y);
            let candidate = (sad, x, y);
            if sad <= threshold {
                accepted.push(candidate);
            }
            if best.is_none_or(|current| candidate < current) {
                best = Some(candidate);
            }
        }
    }

    let (sad, x, y) = best.expect("validated search region has a candidate");
    let ambiguous = accepted.iter().any(|&(_, other_x, other_y)| {
        (other_x, other_y) != (x, y)
            && !rectangles_intersect(
                x,
                y,
                other_x,
                other_y,
                request.template_width,
                request.template_height,
            )
    });
    let status = if ambiguous {
        "ambiguous"
    } else if sad <= threshold {
        "matched"
    } else {
        "rejected"
    };
    json_result(
        status,
        sad,
        count,
        x,
        y,
        request.template_width,
        request.template_height,
    );
}

fn run() -> Result<(), String> {
    let mut input = Vec::new();
    io::stdin()
        .take((MAX_INPUT_BYTES + 1) as u64)
        .read_to_end(&mut input)
        .map_err(|error| format!("cannot read stdin: {error}"))?;
    if input.len() > MAX_INPUT_BYTES {
        return Err("input exceeds 64 MiB".to_string());
    }
    let request = parse_request(&input)?;
    execute(&request);
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("template_matcher: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_request, HEADER_LEN, MAGIC, SEARCH};

    fn request(
        frame_width: u32,
        frame_height: u32,
        template_width: u32,
        template_height: u32,
    ) -> Vec<u8> {
        let mut payload = Vec::from(*MAGIC);
        for value in [
            SEARCH,
            0,
            frame_width,
            frame_height,
            template_width,
            template_height,
            0,
            0,
            frame_width,
            frame_height,
        ] {
            payload.extend_from_slice(&value.to_le_bytes());
        }
        payload.extend(std::iter::repeat_n(
            0,
            (frame_width * frame_height * 3) as usize,
        ));
        payload.extend(std::iter::repeat_n(
            0,
            (template_width * template_height * 3) as usize,
        ));
        payload
    }

    #[test]
    fn parses_a_bounded_request() {
        let payload = request(2, 2, 1, 1);
        let parsed = parse_request(&payload).expect("valid request");
        assert_eq!(parsed.frame.len(), 12);
        assert_eq!(parsed.template.len(), 3);
    }

    #[test]
    fn rejects_trailing_bytes() {
        let mut payload = request(2, 2, 1, 1);
        payload.push(0);
        assert_eq!(
            parse_request(&payload).err().as_deref(),
            Some("input has trailing bytes")
        );
    }

    #[test]
    fn rejects_dimensions_above_fixed_bounds() {
        let payload = request(4097, 1, 1, 1);
        assert_eq!(
            parse_request(&payload).err().as_deref(),
            Some("frame dimensions are outside the fixed bounds")
        );
        assert_eq!(HEADER_LEN, 48);
    }
}
