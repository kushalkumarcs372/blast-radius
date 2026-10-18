"""The fictional company's architecture: services, dependencies and team ownership.

This is hand-written (not random) so the demo scenarios are predictable.
All data in this project is synthetic, as the hackathon rules require.
"""

# name -> (tier, language, owning team)
SERVICES: dict[str, tuple[str, str, str]] = {
    # edge
    "web-frontend":       ("edge", "typescript", "web"),
    "mobile-bff":         ("edge", "go", "mobile"),
    "api-gateway":        ("edge", "go", "platform"),
    "admin-portal":       ("edge", "typescript", "web"),
    # core business
    "checkout":           ("core", "java", "commerce"),
    "cart":               ("core", "java", "commerce"),
    "orders":             ("core", "java", "commerce"),
    "payments":           ("core", "go", "payments"),
    "fraud-check":        ("core", "python", "payments"),
    "refunds":            ("core", "go", "payments"),
    "users":              ("core", "go", "identity"),
    "auth":               ("core", "go", "identity"),
    "catalog":            ("core", "python", "discovery"),
    "search":             ("core", "python", "discovery"),
    "recommendations":    ("core", "python", "discovery"),
    "pricing":            ("core", "java", "commerce"),
    "inventory":          ("core", "java", "fulfilment"),
    "shipping":           ("core", "go", "fulfilment"),
    "notifications":      ("core", "typescript", "platform"),
    "reviews":            ("core", "python", "discovery"),
    "loyalty":            ("core", "java", "commerce"),
    "reporting":          ("core", "python", "data"),
    # data and platform
    "postgres-payments":  ("data", "sql", "payments"),
    "postgres-orders":    ("data", "sql", "commerce"),
    "postgres-users":     ("data", "sql", "identity"),
    "redis-cache":        ("data", "redis", "platform"),
    "elasticsearch":      ("data", "search", "discovery"),
    "kafka":              ("data", "streaming", "data"),
    "warehouse":          ("data", "sql", "data"),
    "feature-flags":      ("platform", "go", "platform"),
    "config-service":     ("platform", "go", "platform"),
    "token-store":        ("data", "redis", "identity"),
    "object-storage":     ("data", "blob", "platform"),
    # third-party adapters
    "stripe-adapter":     ("external", "go", "payments"),
    "sms-gateway":        ("external", "typescript", "platform"),
    "email-gateway":      ("external", "typescript", "platform"),
    "carrier-adapter":    ("external", "go", "fulfilment"),
    "ml-model-server":    ("platform", "python", "data"),
}

# (service, depends_on)
DEPENDENCIES: list[tuple[str, str]] = [
    ("web-frontend", "api-gateway"),
    ("mobile-bff", "api-gateway"),
    ("admin-portal", "api-gateway"),
    ("admin-portal", "reporting"),
    ("api-gateway", "auth"),
    ("api-gateway", "checkout"),
    ("api-gateway", "cart"),
    ("api-gateway", "search"),
    ("api-gateway", "catalog"),
    ("api-gateway", "users"),
    ("api-gateway", "orders"),
    ("api-gateway", "reviews"),
    ("api-gateway", "feature-flags"),
    ("checkout", "cart"),
    ("checkout", "payments"),
    ("checkout", "orders"),
    ("checkout", "pricing"),
    ("checkout", "inventory"),
    ("checkout", "loyalty"),
    ("cart", "pricing"),
    ("cart", "redis-cache"),
    ("orders", "postgres-orders"),
    ("orders", "kafka"),
    ("orders", "shipping"),
    ("payments", "postgres-payments"),
    ("payments", "fraud-check"),
    ("payments", "stripe-adapter"),
    ("payments", "kafka"),
    ("refunds", "payments"),
    ("refunds", "postgres-payments"),
    ("fraud-check", "ml-model-server"),
    ("fraud-check", "users"),
    ("loyalty", "postgres-payments"),
    ("users", "postgres-users"),
    ("auth", "users"),
    ("auth", "token-store"),
    ("catalog", "elasticsearch"),
    ("catalog", "object-storage"),
    ("search", "elasticsearch"),
    ("search", "recommendations"),
    ("recommendations", "ml-model-server"),
    ("pricing", "redis-cache"),
    ("pricing", "config-service"),
    ("inventory", "postgres-orders"),
    ("shipping", "carrier-adapter"),
    ("shipping", "notifications"),
    ("notifications", "kafka"),
    ("notifications", "sms-gateway"),
    ("notifications", "email-gateway"),
    ("reviews", "postgres-users"),
    ("reporting", "warehouse"),
    ("reporting", "kafka"),
    ("ml-model-server", "object-storage"),
    ("feature-flags", "config-service"),
]

TEAMS: dict[str, str] = {
    # team -> chat channel
    "web": "#team-web",
    "mobile": "#team-mobile",
    "platform": "#team-platform",
    "commerce": "#team-commerce",
    "payments": "#team-payments",
    "identity": "#team-identity",
    "discovery": "#team-discovery",
    "fulfilment": "#team-fulfilment",
    "data": "#team-data",
}

FIRST_NAMES = [
    "Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vikram", "Isha", "Arjun", "Priya",
    "Sam", "Lena", "Omar", "Grace", "Mateo", "Yuki", "Noah", "Zara", "Leo", "Nina",
    "Tariq", "Elena", "Ravi", "Chloe", "Hugo", "Amara", "Ivan", "Sofia", "Dev", "Maya",
    "Felix", "Aisha", "Kenji", "Lucia", "Ben", "Tara",
]
LAST_NAMES = [
    "Sharma", "Iyer", "Rao", "Nair", "Gupta", "Menon", "Patel", "Das", "Kulkarni", "Reddy",
    "Chen", "Okafor", "Silva", "Novak", "Haddad", "Tanaka", "Muller", "Kim", "Costa", "Ali",
]
ROLES = ["Engineer", "Senior Engineer", "Staff Engineer", "Engineering Manager", "SRE"]

CHANGE_TEMPLATES = [
    "Bump dependency versions",
    "Refactor request handler",
    "Add metrics for latency",
    "Tune retry and timeout settings",
    "Raise connection pool size",
    "Change cache TTL",
    "Schema migration: add column",
    "Feature flag rollout",
    "Fix null pointer in edge case",
    "Upgrade runtime version",
]

INCIDENT_PATTERNS = [
    ("Connection pool exhausted", "SEV2"),
    ("Elevated 5xx error rate", "SEV2"),
    ("Latency spike on p99", "SEV3"),
    ("Consumer lag growing", "SEV3"),
    ("Memory leak after deploy", "SEV2"),
    ("Bad config pushed", "SEV1"),
    ("Certificate expired", "SEV1"),
    ("Disk filling up", "SEV3"),
]

# Hand-written design decisions, each with a source document (Track 03).
DECISIONS = [
    ("Separate token store for auth", "auth",
     "Session tokens moved out of postgres-users into token-store, a dedicated Redis store, to cut login latency and isolate auth from user-profile load.",
     "ADR-007: Token storage for auth", "adr"),
    ("Payments uses its own database", "payments",
     "Payments data is kept in postgres-payments, isolated from orders, for PCI scope reduction and independent failover.",
     "ADR-003: Payments data isolation", "adr"),
    ("Fraud check is synchronous", "fraud-check",
     "Fraud scoring runs inline during payment authorisation; async scoring caused chargebacks in 2025.",
     "ADR-011: Synchronous fraud scoring", "adr"),
    ("Kafka as the event backbone", "kafka",
     "All order and payment events go through Kafka so downstream consumers never call core services directly.",
     "ADR-002: Event backbone", "adr"),
    ("Cache prices in Redis", "pricing",
     "Prices are cached for 60 seconds in redis-cache; pricing rules change rarely and checkout is latency-sensitive.",
     "ADR-014: Price caching", "adr"),
    ("Gateway owns rate limiting", "api-gateway",
     "Rate limiting is enforced only at api-gateway so backend services stay simple.",
     "ADR-005: Rate limiting at the edge", "adr"),
]
