from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape

import requests
import streamlit as st
from ebaysdk.trading import Connection


st.set_page_config(
    page_title="Card Command | Repricer",
    page_icon="🃏",
    layout="wide",
    initial_sidebar_state="expanded",
)

CENT = Decimal("0.01")
COMPSNIPER_URL = "https://api.compsniper.com/v1/scrape"
REQUIRED_SECRETS = (
    "COMPSNIPER_API_KEY",
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
    "EBAY_DEV_ID",
    "EBAY_USER_TOKEN",
)

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

html, body, [class*="css"], [data-testid="stApp"] {
    font-family: 'Inter', sans-serif;
}
.block-container {
    max-width: 1480px;
    padding-top: 2rem;
    padding-bottom: 4rem;
}
[data-testid="stSidebar"] {
    border-right: 1px solid rgba(128,128,128,.15);
}
[data-testid="stVerticalBlockBorderWrapper"] > div {
    border-radius: 18px !important;
}
[data-testid="stMetric"] {
    padding: .45rem .15rem;
}
[data-testid="stMetricLabel"] {
    opacity: .7;
    font-size: .82rem;
}
[data-testid="stMetricValue"] {
    font-weight: 750;
    letter-spacing: -.045em;
}
.stButton > button {
    border-radius: 10px;
    min-height: 2.7rem;
    font-weight: 600;
}
.stButton > button[kind="primary"] {
    background: #6d4aff;
    border-color: #6d4aff;
    color: white;
}
.stButton > button[kind="primary"]:hover {
    background: #5935e8;
    border-color: #5935e8;
    color: white;
}
button[data-baseweb="tab"] {
    font-weight: 600;
    padding: .9rem 1.1rem;
}
[data-baseweb="tab-highlight"] {
    background-color: #8b70ff;
}
.brand-eyebrow {
    color: #957eff;
    text-transform: uppercase;
    letter-spacing: .18em;
    font-size: .72rem;
    font-weight: 800;
    margin-bottom: .5rem;
}
.brand-title {
    font-size: clamp(2rem, 4vw, 3rem);
    font-weight: 800;
    letter-spacing: -.055em;
    line-height: 1.1;
    margin: 0 0 .65rem;
}
.brand-subtitle {
    opacity: .65;
    font-size: .95rem;
    margin-bottom: 1.5rem;
}
.card-title {
    font-weight: 700;
    font-size: 1.04rem;
    line-height: 1.5;
    margin-bottom: .5rem;
    overflow-wrap: anywhere;
}
.card-placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: .65rem;
    min-height: 160px;
    border-radius: 14px;
    background: linear-gradient(145deg, rgba(139,112,255,.17),
                                         rgba(54,211,184,.06));
    border: 1px solid rgba(139,112,255,.22);
    color: #a390ff;
}
.card-placeholder .symbol {
    font-size: 2.8rem;
    line-height: 1;
}
.card-placeholder .label {
    font-size: .64rem;
    letter-spacing: .12em;
    text-transform: uppercase;
}
.tags {
    display: flex;
    flex-wrap: wrap;
    gap: .4rem;
    margin: .65rem 0;
}
.tag {
    border-radius: 999px;
    padding: .27rem .6rem;
    font-size: .69rem;
    font-weight: 650;
    background: rgba(139,112,255,.12);
    color: #a390ff;
}
.tag-amber {
    background: rgba(230,166,52,.13);
    color: #bb851f;
}
.tag-green {
    background: rgba(32,184,137,.13);
    color: #20a77e;
}
.tag-neutral {
    background: rgba(128,128,128,.12);
    color: inherit;
    opacity: .75;
}
.empty-state {
    text-align: center;
    padding: 3rem 1rem;
    border-radius: 18px;
    background: rgba(139,112,255,.045);
    border: 1px dashed rgba(139,112,255,.28);
}
.empty-state h3 {
    letter-spacing: -.03em;
}
.empty-state p {
    opacity: .65;
}
</style>
"""


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def decimal_price(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("#text"))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Missing or invalid price.") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("Price must be positive and finite.")
    return result


def parse_date(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def get_secret(name):
    try:
        return st.secrets.get(name, "")
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return ""


def safe_error(error):
    message = str(error)
    for name in REQUIRED_SECRETS:
        secret = str(get_secret(name))
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:1200]


def ebay_connection():
    return Connection(
        appid=st.secrets["EBAY_APP_ID"],
        certid=st.secrets["EBAY_CERT_ID"],
        devid=st.secrets["EBAY_DEV_ID"],
        token=st.secrets["EBAY_USER_TOKEN"],
        domain="api.ebay.com",
        siteid="0",
        config_file=None,
        timeout=45,
    )


def check_ebay_response(response):
    if response.get("Ack") not in {"Success", "Warning"}:
        errors = as_list(response.get("Errors"))
        messages = [
            str(error.get("LongMessage") or error.get("ShortMessage"))
            for error in errors
            if isinstance(error, dict)
        ]
        raise RuntimeError("; ".join(messages) or "eBay rejected the request.")


def fetch_unsold_inventory():
    api = ebay_connection()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    inventory = {}
    page = 1

    while True:
        response = api.execute(
            "GetMyeBaySelling",
            {
                "DetailLevel": "ReturnAll",
                "ActiveList": {
                    "Include": True,
                    "Pagination": {
                        "EntriesPerPage": 200,
                        "PageNumber": page,
                    },
                },
            },
        ).dict()
        check_ebay_response(response)

        active = response.get("ActiveList") or {}
        items = as_list((active.get("ItemArray") or {}).get("Item"))

        for item in items:
            item_id = str(item.get("ItemID", ""))
            if not item_id:
                continue

            status = item.get("SellingStatus") or {}
            sold = int(status.get("QuantitySold", 0) or 0)
            quantity = int(item.get("Quantity", 0) or 0)

            if quantity <= sold:
                continue

            start = parse_date(
                (item.get("ListingDetails") or {}).get("StartTime")
            )
            raw_watchers = item.get("WatchCount")
            watchers = int(raw_watchers) if raw_watchers is not None else None

            if watchers != 0 and not (start and start < cutoff):
                continue

            raw_price = status.get("CurrentPrice")
            try:
                price = decimal_price(raw_price)
            except ValueError:
                continue

            currency = (
                raw_price.get("_currencyID")
                or raw_price.get("@currencyID")
                or raw_price.get("currencyID")
                if isinstance(raw_price, dict)
                else None
            ) or item.get("Currency")

            inventory[item_id] = {
                "item_id": item_id,
                "title": str(item.get("Title", "")),
                "current_price": price,
                "currency": currency,
                "watchers": watchers,
                "age_days": (
                    (datetime.now(timezone.utc) - start).days if start else None
                ),
                "listing_type": item.get("ListingType"),
                "has_variations": bool(item.get("Variations")),
            }

        total_pages = int(
            (active.get("PaginationResult") or {}).get(
                "TotalNumberOfPages", 0
            ) or 0
        )
        if page >= total_pages:
            break
        page += 1

    return list(inventory.values())


def analyze_comps(title):
    try:
        response = requests.get(
            COMPSNIPER_URL,
            headers={
                "Authorization": f"Bearer {st.secrets['COMPSNIPER_API_KEY']}",
                "Accept": "application/json",
            },
            params={
                "keyword": title,
                "count": 10,
                "limit": 10,
                "bestOfferPrice": "true",
                "sold": "true",
                "sortOrder": "endedRecently",
                "ebaySite": "ebay.com",
                "itemLocation": "domestic",
            },
            timeout=(10, 90),
            allow_redirects=False,
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("CompSniper did not return a successful JSON response.")
        payload = response.json()
    except requests.Timeout:
        raise RuntimeError("CompSniper timed out. Try again.") from None
    except requests.RequestException as error:
        code = getattr(error.response, "status_code", None)
        raise RuntimeError(
            f"CompSniper request failed (HTTP {code or 'unavailable'})."
        ) from None
    except ValueError:
        raise RuntimeError("CompSniper returned an invalid response.") from None

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("CompSniper response is missing its items array.")

    sales = sorted(
        [row for row in payload["items"] if isinstance(row, dict)],
        key=lambda row: parse_date(row.get("endedAt"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:10]

    prices = []
    evidence = []
    seen = set()

    for sale in sales:
        item_id = sale.get("itemId")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)

        if sale.get("soldCurrency") != "USD":
            continue

        accepted = sale.get("bestOfferAccepted") in (True, "true")
        if accepted:
            if sale.get("acceptedOfferPriceStatus") != "found":
                continue
            raw_price = sale.get("acceptedOfferPrice")
        else:
            raw_price = sale.get("soldPrice")

        try:
            price = decimal_price(raw_price)
        except ValueError:
            continue

        prices.append(price)
        evidence.append(
            {
                "Title": sale.get("title"),
                "Sale date": sale.get("endedAt"),
                "Price (USD)": float(price),
                "Accepted offer": accepted,
            }
        )

    if len(prices) < 3:
        raise ValueError(
            "At least 3 valid USD sales are required after excluding "
            "unresolved Best Offers."
        )

    cleaned = sorted(prices)[1:-1]
    return {
        "average": sum(cleaned, Decimal("0")) / Decimal(len(cleaned)),
        "count": len(prices),
        "cleaned_count": len(cleaned),
        "evidence": evidence,
        "analyzed_at": datetime.now(timezone.utc),
    }


def update_ebay_listing(item_id, target_price, expected_price):
    try:
        price = decimal_price(target_price)
        if price != price.quantize(CENT):
            raise ValueError("Target price must have at most two decimal places.")

        api = ebay_connection()
        current = api.execute(
            "GetItem",
            {"ItemID": str(item_id), "DetailLevel": "ReturnAll"},
        ).dict()
        check_ebay_response(current)
        item = current.get("Item") or {}
        status = item.get("SellingStatus") or {}

        if status.get("ListingStatus") != "Active":
            raise ValueError("This listing is no longer active.")
        if item.get("ListingType") != "FixedPriceItem":
            raise ValueError("Only fixed-price listings can be updated.")
        if item.get("Variations"):
            raise ValueError("Variation listings require individual variation pricing.")
        if item.get("Currency") != "USD":
            raise ValueError("Only USD listings can use these USD recommendations.")
        if str(item.get("Title", "")) != st.session_state.inventory_by_id[
            str(item_id)
        ]["title"]:
            raise ValueError("The listing title changed. Refresh and analyze again.")

        live_price = decimal_price(status.get("CurrentPrice"))
        if live_price != expected_price:
            raise ValueError(
                "The live price changed. Refresh inventory and analyze again."
            )

        details = item.get("ListingDetails") or {}
        thresholds = {}
        for field in ("MinimumBestOfferPrice", "BestOfferAutoAcceptPrice"):
            value = details.get(field)
            if value is not None:
                amount = Decimal(
                    str(value.get("value") if isinstance(value, dict) else value)
                )
                if amount > 0:
                    if amount >= price:
                        raise ValueError(
                            "The target conflicts with an existing Best Offer "
                            "threshold. Adjust that threshold on eBay first."
                        )
                    thresholds[field] = format(amount, ".2f")

        revision = {
            "ItemID": str(item_id),
            "StartPrice": format(price, ".2f"),
        }
        if thresholds:
            revision["ListingDetails"] = thresholds

        # Never retry a write automatically; a timeout may follow a successful write.
        response = api.execute(
            "ReviseFixedPriceItem", {"Item": revision}
        ).dict()
        check_ebay_response(response)

        warnings = [
            str(error.get("LongMessage") or error.get("ShortMessage"))
            for error in as_list(response.get("Errors"))
            if isinstance(error, dict)
        ]
        return True, "; ".join(warnings)
    except Exception as error:
        return False, safe_error(error)


def supported_listing(item):
    return (
        item["listing_type"] == "FixedPriceItem"
        and not item["has_variations"]
        and item["currency"] == "USD"
    )


def analysis_expired(analysis):
    return (
        datetime.now(timezone.utc) - analysis["analyzed_at"]
    ) > timedelta(minutes=30)


def recommended_price(analysis, markdown, floor):
    return max(
        floor,
        (
            analysis["average"]
            * (Decimal("1") - Decimal(markdown) / Decimal("100"))
        ).quantize(CENT, rounding=ROUND_HALF_UP),
    )


def money(value):
    return f"${value:,.2f}"


def tags_html(tags):
    return '<div class="tags">' + "".join(
        f'<span class="tag {escape(style)}">{escape(label)}</span>'
        for label, style in tags
    ) + "</div>"


def render_sidebar():
    with st.sidebar:
        st.markdown('<div class="brand-eyebrow">The Lot Spot</div>',
                    unsafe_allow_html=True)
        st.subheader("Pricing controls")
        st.caption("Fine-tune every recommendation.")

        with st.container(border=True):
            markdown = st.slider(
                "Markdown from trimmed average (%)",
                0, 50, 5,
                key="markdown_percentage",
            )
            floor = Decimal(str(st.number_input(
                "Minimum target price (USD)",
                min_value=0.01,
                value=0.99,
                step=0.01,
                format="%.2f",
                key="minimum_target",
            )))

        st.caption(
            "Targets update automatically when these controls change. "
            "Live prices change only after you confirm and select Update Price."
        )
        st.divider()
        st.markdown("**Your pricing workflow**")
        st.caption("01 · Refresh eligible inventory")
        st.caption("02 · Analyze and review recent sales")
        st.caption("03 · Confirm and update your price")
        st.divider()
        st.caption(
            "Prices exclude shipping and fees. Review card identity, condition, "
            "language, and quantity before updating."
        )
    return markdown, floor


def render_summary(inventory, markdown, floor):
    items = inventory or []
    valid_analyses = [
        (item, st.session_state.comps[item["item_id"]])
        for item in items
        if supported_listing(item)
        and item["item_id"] in st.session_state.comps
        and not analysis_expired(st.session_state.comps[item["item_id"]])
    ]
    attention = sum(
        recommended_price(analysis, markdown, floor) != item["current_price"]
        and item["item_id"] not in st.session_state.blocked_updates
        for item, analysis in valid_analyses
    )
    reductions = sum(
        (
            max(
                Decimal("0"),
                item["current_price"] - recommended_price(analysis, markdown, floor),
            )
            for item, analysis in valid_analyses
        ),
        Decimal("0"),
    )

    columns = st.columns(4)
    values = [
        ("Eligible active listings", str(len(items)) if inventory is not None else "—",
         "Zero watchers or listed over 30 days"),
        ("Price changes ready", str(attention) if inventory is not None else "—",
         "Fresh recommendations awaiting review"),
        ("Listings analyzed", str(len(valid_analyses)),
         "Recommendations valid for 30 minutes"),
        ("Suggested price reductions", money(reductions),
         "One unit per listing · not estimated profit"),
    ]
    for column, (label, value, caption) in zip(columns, values):
        with column:
            with st.container(border=True):
                st.metric(label, value)
                st.caption(caption)


def render_listing(item, markdown, floor):
    item_id = item["item_id"]
    supported = supported_listing(item)

    with st.container(border=True):
        image_column, content_column, action_column = st.columns(
            [1, 3.3, 1.8], gap="large"
        )

        with image_column:
            st.markdown(
                '<div class="card-placeholder">'
                '<span class="symbol">▧</span>'
                '<span class="label">Card preview</span></div>',
                unsafe_allow_html=True,
            )

        with content_column:
            st.markdown(
                f'<div class="card-title">{escape(item["title"])}</div>',
                unsafe_allow_html=True,
            )
            st.caption(f"eBay item · {item_id}")

            tags = []
            if item["watchers"] == 0:
                tags.append(("Zero watchers", "tag-amber"))
            if item["age_days"] is not None:
                tags.append((
                    f'{item["age_days"]} days listed',
                    "tag-amber" if item["age_days"] > 30 else "tag-neutral",
                ))
            if item["watchers"] is None:
                tags.append(("Watchers unknown", "tag-neutral"))
            elif item["watchers"] > 0:
                tags.append((f'{item["watchers"]} watchers', "tag-neutral"))
            tags.append((
                "Single-price listing" if supported else "Manual pricing",
                "tag-green" if supported else "tag-neutral",
            ))
            st.markdown(tags_html(tags), unsafe_allow_html=True)

        with action_column:
            st.caption("MARKET RESEARCH")
            if st.button(
                "Analyze Comps",
                key=f"analyze_{item_id}",
                disabled=not supported,
                width="stretch",
            ):
                st.session_state.comps.pop(item_id, None)
                st.session_state.notices.pop(item_id, None)
                try:
                    with st.status(
                        "Analyzing recent sold prices…", expanded=True
                    ) as status:
                        st.write("Requesting up to 10 recent sales.")
                        analysis = analyze_comps(item["title"])
                        st.session_state.comps[item_id] = analysis
                        status.update(
                            label=f'{analysis["cleaned_count"]} comparable prices retained',
                            state="complete",
                            expanded=False,
                        )
                    st.rerun()
                except Exception as error:
                    status.update(
                        label="Analysis could not be completed",
                        state="error",
                        expanded=False,
                    )
                    st.error(safe_error(error))

            if not supported:
                st.caption("Requires a single-price USD fixed-price listing.")

        analysis = st.session_state.comps.get(item_id)
        target = recommended_price(analysis, markdown, floor) if analysis else None
        expired = analysis_expired(analysis) if analysis else False
        blocked = item_id in st.session_state.blocked_updates

        with content_column:
            prices = st.columns(3)
            prices[0].metric(
                "Current price",
                f'{item["current_price"]:,.2f} {item["currency"] or "?"}',
            )
            prices[1].metric(
                "Trimmed average",
                money(analysis["average"]) if analysis else "—",
            )
            prices[2].metric(
                "Recommended target",
                money(target) if target is not None else "—",
                delta=(
                    f'{target - item["current_price"]:+.2f} USD'
                    if target is not None and target != item["current_price"]
                    else None
                ),
                delta_color="off",
            )
            if analysis:
                st.caption(
                    f'{analysis["count"]} valid sales · '
                    f'{analysis["cleaned_count"]} retained after removing '
                    f'highest and lowest · {markdown}% markdown'
                )
                with st.expander("Review comparable sales"):
                    st.caption(
                        "The table includes all valid sales before trimming. "
                        "One highest and one lowest price are excluded from the average."
                    )
                    st.dataframe(
                        analysis["evidence"],
                        hide_index=True,
                        width="stretch",
                    )
                    st.caption(
                        "Analyzed "
                        + analysis["analyzed_at"].strftime("%b %d, %Y · %H:%M UTC")
                    )
            else:
                st.caption("Analyze comps to generate a market-based target.")

        with action_column:
            if supported and analysis:
                st.divider()
                if expired:
                    st.warning("Recommendation expired. Analyze again.")
                elif blocked:
                    st.info("Refresh inventory before another update.")
                elif target == item["current_price"]:
                    st.success("Current price matches the target.")
                else:
                    st.caption("LIVE EBAY UPDATE")

                confirmed = st.checkbox(
                    f'Confirm {money(item["current_price"])} → {money(target)}',
                    key=(
                        f"confirm_{item_id}_{target}_"
                        f'{analysis["analyzed_at"].isoformat()}'
                    ),
                    disabled=expired or blocked or target == item["current_price"],
                )

                if st.button(
                    "Update Price",
                    key=f"update_{item_id}",
                    type="primary",
                    width="stretch",
                    disabled=(
                        not confirmed
                        or expired
                        or blocked
                        or target == item["current_price"]
                    ),
                ):
                    st.session_state.blocked_updates.add(item_id)
                    with st.status(
                        "Updating live eBay price…", expanded=True
                    ) as status:
                        st.write("Checking listing details and current price.")
                        success, message = update_ebay_listing(
                            item_id, target, item["current_price"]
                        )
                        if success:
                            item["current_price"] = target
                            st.session_state.notices[item_id] = {
                                "success": True,
                                "message": f"Updated to {money(target)}.",
                                "warning": message,
                            }
                            status.update(
                                label="Price updated successfully",
                                state="complete",
                                expanded=False,
                            )
                        else:
                            st.session_state.notices[item_id] = {
                                "success": False,
                                "message": message,
                                "warning": "",
                            }
                            status.update(
                                label="Update requires review",
                                state="error",
                                expanded=False,
                            )
                    st.rerun()

            notice = st.session_state.notices.get(item_id)
            if notice:
                if notice["success"]:
                    st.success(notice["message"])
                    if notice["warning"]:
                        st.warning(notice["warning"])
                else:
                    st.error(notice["message"])
                    st.caption(
                        "Check the live listing, then refresh inventory before retrying."
                    )


def render_inventory(inventory, markdown, floor, configured):
    heading, refresh = st.columns([3, 1])
    with heading:
        st.subheader("Inventory Management")
        st.caption("Find quiet listings. Review the market. Price with confidence.")
    with refresh:
        if st.button(
            "Refresh Inventory",
            type="primary",
            width="stretch",
            disabled=not configured,
        ):
            try:
                with st.status(
                    "Syncing active eBay inventory…", expanded=True
                ) as status:
                    st.write("Checking remaining stock, watchers, and listing age.")
                    fetched = fetch_unsold_inventory()
                    st.session_state.inventory = fetched
                    st.session_state.inventory_by_id = {
                        item["item_id"]: item for item in fetched
                    }
                    st.session_state.comps = {}
                    st.session_state.blocked_updates = set()
                    st.session_state.notices = {}
                    st.session_state.last_sync = datetime.now(timezone.utc)
                    status.update(
                        label=f"{len(fetched)} eligible listings loaded",
                        state="complete",
                        expanded=False,
                    )
                st.rerun()
            except Exception as error:
                status.update(
                    label="Inventory sync failed",
                    state="error",
                    expanded=False,
                )
                st.error(safe_error(error))

    if not configured:
        st.warning("Add the required secrets to get started. See API Settings.")
        return

    if inventory is None:
        st.markdown(
            '<div class="empty-state"><h3>Your next pricing move starts here.</h3>'
            '<p>Refresh inventory to load eligible active eBay listings.</p></div>',
            unsafe_allow_html=True,
        )
        return

    if not inventory:
        st.success("No active listings match the watcher or listing-age filter.")
        return

    sync = st.session_state.last_sync
    st.caption(
        f"{len(inventory)} eligible active listings with remaining stock. "
        "Listing age does not measure time since the last sale."
        + (f" Last sync: {sync:%b %d · %H:%M UTC}." if sync else "")
    )

    search_column, filter_column, sort_column = st.columns([2.3, 1.3, 1.3])
    search = search_column.text_input(
        "Search inventory",
        placeholder="Search card name, set, or listing title…",
    ).strip().casefold()
    view = filter_column.selectbox(
        "Show",
        ["All eligible listings", "Ready to update", "Needs analysis", "Manual pricing"],
    )
    order = sort_column.selectbox(
        "Sort by",
        ["Oldest first", "Price: high to low", "Price: low to high", "Title"],
    )

    filtered = []
    for item in inventory:
        if search not in item["title"].casefold():
            continue
        analysis = st.session_state.comps.get(item["item_id"])
        fresh = analysis is not None and not analysis_expired(analysis)
        if view == "Ready to update" and not (
            supported_listing(item)
            and fresh
            and item["item_id"] not in st.session_state.blocked_updates
            and recommended_price(analysis, markdown, floor) != item["current_price"]
        ):
            continue
        if view == "Needs analysis" and not (
            supported_listing(item) and not fresh
        ):
            continue
        if view == "Manual pricing" and supported_listing(item):
            continue
        filtered.append(item)

    if order == "Oldest first":
        filtered.sort(
            key=lambda item: item["age_days"] if item["age_days"] is not None else -1,
            reverse=True,
        )
    elif order == "Price: high to low":
        filtered.sort(key=lambda item: item["current_price"], reverse=True)
    elif order == "Price: low to high":
        filtered.sort(key=lambda item: item["current_price"])
    else:
        filtered.sort(key=lambda item: item["title"].casefold())

    if not filtered:
        st.info("No listings match these filters.")
        return

    page_count = max(1, (len(filtered) + 19) // 20)
    page = st.selectbox(
        "Page",
        range(1, page_count + 1),
        format_func=lambda value: f"Page {value} of {page_count}",
    )
    start = (page - 1) * 20
    st.caption(
        f"Showing {start + 1}–{min(start + 20, len(filtered))} "
        f"of {len(filtered)} listings"
    )

    for item in filtered[start:start + 20]:
        render_listing(item, markdown, floor)


def render_analytics(inventory, markdown, floor):
    st.subheader("Pricing Analytics")
    st.caption("Compare your current prices with reviewed market recommendations.")

    rows = []
    for item in inventory or []:
        analysis = st.session_state.comps.get(item["item_id"])
        if not analysis or not supported_listing(item):
            continue
        target = recommended_price(analysis, markdown, floor)
        rows.append({
            "Listing": item["title"],
            "Item ID": item["item_id"],
            "Current price": float(item["current_price"]),
            "Trimmed average": float(analysis["average"]),
            "Target price": float(target),
            "Change": float(target - item["current_price"]),
            "Sales retained": analysis["cleaned_count"],
            "Status": "Expired" if analysis_expired(analysis) else "Fresh",
        })

    if not rows:
        st.info("Analyze a listing in Inventory Management to populate analytics.")
        return

    fresh = [row for row in rows if row["Status"] == "Fresh"]
    left, right = st.columns([2, 1], gap="large")

    with left:
        with st.container(border=True):
            st.markdown("**Current price vs. recommended target**")
            if fresh:
                chart_rows = [
                    {
                        "Listing": f'{row["Listing"][:48]} · {row["Item ID"]}',
                        "Current price": row["Current price"],
                        "Target price": row["Target price"],
                    }
                    for row in fresh[:20]
                ]
                st.bar_chart(
                    chart_rows,
                    x="Listing",
                    y=["Current price", "Target price"],
                    stack=False,
                    color=["#8b70ff", "#35c9a5"],
                )
                st.caption("Up to 20 fresh recommendations · USD per unit.")
            else:
                st.info("Analyze listings again to refresh expired recommendations.")

    with right:
        with st.container(border=True):
            st.markdown("**Recommendation coverage**")
            eligible = sum(supported_listing(item) for item in inventory or [])
            coverage = len(fresh) / eligible if eligible else 0
            st.progress(min(coverage, 1.0))
            st.caption(f"{len(fresh)} of {eligible} supported listings analyzed.")
            st.metric(
                "Suggested decreases",
                sum(row["Change"] < 0 for row in fresh),
            )
            st.metric(
                "Suggested increases",
                sum(row["Change"] > 0 for row in fresh),
            )
            st.caption(
                "Price differences are not profit estimates. "
                "Costs, fees, shipping, and sales volume are not included."
            )

    with st.container(border=True):
        st.markdown("**Recommendation details**")
        st.dataframe(
            rows,
            hide_index=True,
            width="stretch",
            column_config={
                name: st.column_config.NumberColumn(name, format="$%.2f")
                for name in (
                    "Current price", "Trimmed average", "Target price", "Change"
                )
            },
        )


def render_settings():
    st.subheader("API Settings")
    st.caption("Credential availability and integration details.")

    ebay_column, comps_column = st.columns(2, gap="large")

    with ebay_column:
        with st.container(border=True):
            st.markdown("**eBay Trading API**")
            st.caption("Inventory reads and confirmed fixed-price revisions.")
            for name in REQUIRED_SECRETS[1:]:
                label, state = st.columns([3, 1])
                label.code(name, language=None)
                if get_secret(name):
                    state.success("Present")
                else:
                    state.warning("Missing")
            st.caption(
                "Credential presence does not verify authentication or permissions."
            )

    with comps_column:
        with st.container(border=True):
            st.markdown("**CompSniper**")
            st.caption("Recent sold comparables and resolved accepted offers.")
            label, state = st.columns([3, 1])
            label.code("COMPSNIPER_API_KEY", language=None)
            if get_secret("COMPSNIPER_API_KEY"):
                state.success("Present")
            else:
                state.warning("Missing")
            st.code(COMPSNIPER_URL, language=None)
            st.caption(
                "Up to 10 recent sales. USD only. One highest and one lowest "
                "valid price are removed before averaging."
            )

    with st.container(border=True):
        st.markdown("**Configuration**")
        st.write(
            "Credentials are read exclusively from st.secrets. "
            "Configure them in your deployment's Streamlit secrets file."
        )
        st.code(
            'COMPSNIPER_API_KEY = "YOUR_COMPSNIPER_API_KEY"\n'
            'EBAY_APP_ID = "YOUR_EBAY_APP_ID"\n'
            'EBAY_CERT_ID = "YOUR_EBAY_CERT_ID"\n'
            'EBAY_DEV_ID = "YOUR_EBAY_DEV_ID"\n'
            'EBAY_USER_TOKEN = "YOUR_EBAY_USER_TOKEN"',
            language="toml",
        )
        st.caption("Secret values are never displayed in this interface.")


def main():
    st.markdown(CSS, unsafe_allow_html=True)

    defaults = {
        "inventory": None,
        "inventory_by_id": {},
        "comps": {},
        "blocked_updates": set(),
        "notices": {},
        "last_sync": None,
    }
    for name, value in defaults.items():
        st.session_state.setdefault(name, value)

    st.markdown(
        '<div class="brand-eyebrow">The Lot Spot · Seller Workspace</div>'
        '<div class="brand-title">Card Command</div>'
        '<div class="brand-subtitle">'
        'A clearer view of your inventory. A smarter next price.</div>',
        unsafe_allow_html=True,
    )

    inventory_tab, analytics_tab, settings_tab = st.tabs([
        "Inventory Management", "Pricing Analytics", "API Settings"
    ])

    markdown, floor = render_sidebar()
    missing = [name for name in REQUIRED_SECRETS if not get_secret(name)]
    configured = not missing

    inventory = st.session_state.inventory
    st.session_state.inventory_by_id = {
        item["item_id"]: item for item in inventory or []
    }

    with inventory_tab:
        render_summary(inventory, markdown, floor)
        st.write("")
        render_inventory(inventory, markdown, floor, configured)

    with analytics_tab:
        render_summary(inventory, markdown, floor)
        st.write("")
        render_analytics(inventory, markdown, floor)

    with settings_tab:
        if missing:
            st.warning("Missing secrets: " + ", ".join(missing))
        render_settings()


if __name__ == "__main__":
    main()
