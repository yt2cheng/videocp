from videocp.cli import build_parser


def test_doctor_supports_keep_open_login_urls():
    args = build_parser().parse_args(
        [
            "doctor",
            "--no-headless",
            "--keep-open",
            "--login-url",
            "https://www.douyin.com/",
            "--login-url",
            "https://pd.qq.com/",
        ]
    )

    assert args.command == "doctor"
    assert args.headless is False
    assert args.keep_open is True
    assert args.login_urls == ["https://www.douyin.com/", "https://pd.qq.com/"]
