//! How much of a posted limit actually traded.
//!
//! The bridge wraps the venue payload as `{ "success": true, "response": ... }`.
//! `success` means the order was accepted, not that it filled. A resting GTD
//! on a wide book comes back live with a matched size of zero. Booking the
//! requested size in that case invents inventory.

use serde_json::Value;

/// What the worker may do with a post response.
#[derive(Debug, Clone, PartialEq)]
pub enum PostedFill {
    Rejected {
        order_id: Option<String>,
        error: String,
    },
    /// `tokens` is the size matched by this response, never more than requested.
    /// `resting` means the remainder is still working at the venue.
    Accepted {
        order_id: Option<String>,
        tokens: f64,
        resting: bool,
    },
}

/// Interpret a bridge `/create_and_post_order` body.
///
/// `requested_tokens` is the size we posted. It is used only when the venue
/// says the order matched and does not include a size.
pub fn interpret_post(body: &Value, requested_tokens: f64) -> PostedFill {
    let requested = if requested_tokens.is_finite() && requested_tokens > 0.0 {
        requested_tokens
    } else {
        0.0
    };

    if body.get("success").and_then(|v| v.as_bool()) == Some(false) {
        return PostedFill::Rejected {
            order_id: order_id(body),
            error: error_text(body),
        };
    }

    let venue = body
        .get("response")
        .filter(|v| v.is_object())
        .unwrap_or(body);

    if venue.get("success").and_then(|v| v.as_bool()) == Some(false) {
        return PostedFill::Rejected {
            order_id: order_id(venue).or_else(|| order_id(body)),
            error: error_text(venue),
        };
    }

    let status = venue
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let matched = matched_size(venue);
    let id = order_id(venue).or_else(|| order_id(body));

    if is_dead(&status) {
        let tokens = matched.unwrap_or(0.0).clamp(0.0, requested);
        if tokens > 0.0 {
            return PostedFill::Accepted {
                order_id: id,
                tokens,
                resting: false,
            };
        }
        return PostedFill::Rejected {
            order_id: id,
            error: if status.is_empty() {
                error_text(venue)
            } else {
                status
            },
        };
    }

    let (tokens, resting) = if let Some(size) = matched {
        let tokens = size.clamp(0.0, requested);
        let still_open = is_resting(&status) && requested - tokens > 1e-9;
        (tokens, still_open || (status.is_empty() && tokens <= 1e-9))
    } else if is_matched(&status) {
        (requested, false)
    } else if is_resting(&status) || status.is_empty() {
        // Accepted, with no evidence of a fill. Do not invent one.
        (0.0, true)
    } else {
        (0.0, false)
    };

    PostedFill::Accepted {
        order_id: id,
        tokens,
        resting,
    }
}

fn matched_size(venue: &Value) -> Option<f64> {
    for key in [
        "size_matched",
        "sizeMatched",
        "matchedAmount",
        "matched_amount",
    ] {
        if let Some(value) = venue.get(key).and_then(json_f64) {
            if value.is_finite() {
                return Some(value);
            }
        }
    }
    None
}

fn json_f64(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str().and_then(|text| text.trim().parse().ok()))
}

fn order_id(value: &Value) -> Option<String> {
    for key in ["orderID", "order_id", "id"] {
        if let Some(text) = value.get(key).and_then(|v| v.as_str()) {
            if !text.is_empty() {
                return Some(text.to_string());
            }
        }
    }
    None
}

fn error_text(value: &Value) -> String {
    for key in ["error", "errorMsg", "error_msg"] {
        if let Some(text) = value.get(key).and_then(|v| v.as_str()) {
            if !text.is_empty() {
                return text.to_string();
            }
        }
    }
    "order rejected".to_string()
}

fn is_matched(status: &str) -> bool {
    matches!(status, "matched" | "filled" | "mined" | "confirmed")
}

fn is_resting(status: &str) -> bool {
    matches!(
        status,
        "live" | "delayed" | "unmatched" | "open" | "placed" | "pending"
    )
}

fn is_dead(status: &str) -> bool {
    matches!(status, "cancelled" | "canceled" | "rejected" | "expired")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn live_gtd_does_not_book_the_requested_size() {
        let body = json!({
            "success": true,
            "response": {
                "orderID": "ord-live",
                "status": "live",
                "size_matched": "0"
            }
        });
        assert_eq!(
            interpret_post(&body, 175.0),
            PostedFill::Accepted {
                order_id: Some("ord-live".into()),
                tokens: 0.0,
                resting: true,
            }
        );
    }

    #[test]
    fn missing_size_on_a_live_order_is_not_a_full_fill() {
        let body = json!({
            "success": true,
            "response": { "orderID": "ord-live", "status": "LIVE" }
        });
        match interpret_post(&body, 50.0) {
            PostedFill::Accepted {
                tokens, resting, ..
            } => {
                assert_eq!(tokens, 0.0);
                assert!(resting);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn success_without_a_venue_status_is_not_a_full_fill() {
        let body = json!({ "success": true });
        match interpret_post(&body, 50.0) {
            PostedFill::Accepted {
                tokens, resting, ..
            } => {
                assert_eq!(tokens, 0.0);
                assert!(resting);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn partial_match_books_only_the_matched_size() {
        let body = json!({
            "success": true,
            "response": { "status": "live", "size_matched": "4.5", "orderID": "ord-part" }
        });
        assert_eq!(
            interpret_post(&body, 10.0),
            PostedFill::Accepted {
                order_id: Some("ord-part".into()),
                tokens: 4.5,
                resting: true,
            }
        );
    }

    #[test]
    fn matched_status_without_a_size_books_the_request() {
        let body = json!({
            "success": true,
            "response": { "orderID": "ord-full", "status": "MATCHED" }
        });
        assert_eq!(
            interpret_post(&body, 10.0),
            PostedFill::Accepted {
                order_id: Some("ord-full".into()),
                tokens: 10.0,
                resting: false,
            }
        );
    }

    #[test]
    fn matched_amount_zero_books_nothing() {
        let body = json!({
            "success": true,
            "response": { "orderID": "ord-zero", "status": "MATCHED", "matchedAmount": "0" }
        });
        assert_eq!(
            interpret_post(&body, 10.0),
            PostedFill::Accepted {
                order_id: Some("ord-zero".into()),
                tokens: 0.0,
                resting: false,
            }
        );
    }

    #[test]
    fn matched_size_cannot_exceed_the_request() {
        let body = json!({
            "success": true,
            "response": { "status": "matched", "size_matched": "999" }
        });
        match interpret_post(&body, 10.0) {
            PostedFill::Accepted {
                tokens, resting, ..
            } => {
                assert_eq!(tokens, 10.0);
                assert!(!resting);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn bridge_failure_is_a_reject() {
        let body = json!({ "success": false, "error": "nonce too low" });
        assert_eq!(
            interpret_post(&body, 10.0),
            PostedFill::Rejected {
                order_id: None,
                error: "nonce too low".into(),
            }
        );
    }

    #[test]
    fn cancelled_with_no_match_is_a_reject() {
        let body = json!({
            "success": true,
            "response": { "orderID": "ord-x", "status": "cancelled", "size_matched": "0" }
        });
        match interpret_post(&body, 10.0) {
            PostedFill::Rejected { order_id, .. } => {
                assert_eq!(order_id.as_deref(), Some("ord-x"));
            }
            other => panic!("unexpected {other:?}"),
        }
    }
}
