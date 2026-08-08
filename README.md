# gar-stream-tx — Raspberry Pi 5 + OV3660 USB カメラ（TX 側）

**目的:** [gar-stream-rx](../gar-stream-rx/README.md)（Luckfox Lyra Plus のビデオ
モニター）に MJPEG/RTP を送る TX 側。カメラは

> USBカメラモジュール OV3660搭載｜USB2.0出力・2048×1536／15fps・110°広角｜
> スマホ・タブレット対応OTG（Android/iOS）

を使う。KY-040 ロータリーエンコーダで、送信映像のオンスクリーンメニューを
操作する。これは実機・GARシミュレーションで共通の操作である。

- **押す** → メニューを表示 → 選択項目を編集 → 決定してメニューを閉じる
- **回す** → 項目を選択、または編集中の値を変更

メニューは **Profile**（解像度、全Profileで15fps固定）、**Mirror**（左右反転）、
**Rotate**（0° / 90° / 180° / 270°）、**Overlay**（映像上の状態表示）で構成する。

加えて、**TX 自身に ILI9341 を直結してローカルプレビューを出す**。カメラ入力を
RX に送るのとは別に、同じ変換・Overlay済みフレームを TX 上の SPI パネルにも描画できる
— 詳しくは「アーキテクチャ」と「1. 配線」を参照。ディスプレイを繋がない
場合は `GAR_LOCAL_DISPLAY=0` を指定して無効化できる。

## アーキテクチャ

カメラは常にネイティブ最大モード（2048×1536 @ 15fps MJPEG、製品仕様上ここは
確実に存在する）でキャプチャし、GStreamer の `tee` で経路を分岐する:

- **ネットワーク分岐**: `videoscale/videorate → capsfilter(out_caps) →
  jpegenc → rtpjpegpay → udpsink` で RX（gar-stream-rx）に送る。
- **ローカルプレビュー分岐**（`local_display=True` のときだけ追加）:
  `videoconvert/videoscale → RGB565 → appsink` で Python 側が受け取り、
  ILI9341 に SPI で blit する（gar-stream-rx の `video_monitor.py` と同じ
  appsink → blit の作り）。

```mermaid
flowchart LR
    CAM["OV3660 USB UVC camera\n(native 2048x1536@15fps MJPEG)"] --> DEC["jpegdec"]
    DEC --> TEE["tee"]
    TEE --> NET["videoscale/videorate\n! capsfilter(out_caps)\n! jpegenc"]
    NET --> RTP["rtpjpegpay ! udpsink"] --> RX["gar-stream-rx\n(Lyra Plus) :5600"]
    TEE -. "local_display=True" .-> PREV["videoconvert/videoscale\n! RGB565 ! appsink"]
    PREV -- "SPI blit()" --> ILI["ILI9341\n(on this Pi 5, optional)"]
    KY["KY-040\n(periphery GPIO)"] -- "rotate" --> TX["camera_tx.py"]
    KY -- "press" --> TX
    TX -- "set_property(out_caps.caps)" --> NET
```

GStreamer は **PyGObject** 経由でプロセス内に常駐させ（`gst-launch-1.0` を
サブプロセスとして起動し直す方式ではない）、KY-040 でサイズ/レートを変えても
`out_caps` という名前の `capsfilter` の `caps` プロパティを差し替えるだけで、
ダウンストリーム（videoscale/videorate/jpegenc/rtpjpegpay/udpsink）がライブに
再ネゴシエーションする。カメラのキャプチャ自体（`v4l2src`）は常にネイティブ
モードのままで一切再オープンしないので、パイプライン全体の再起動・RX 側の
瞬断は発生しない。

任意の SIZE プリセットがカメラの UVC ディスクリプタに実在する discrete
モードとは限らないため（未確認）、ネットワーク分岐は常にソフトウェアで
デコード→リサイズ→フレーム間引き→再エンコードする構成にして、
`v4l2-ctl --list-formats-ext` を確認していなくても確実に動く方を優先した。
実際のモード一覧を確認できて、目的のサイズ/レートが native mode として
存在するとわかれば、[gar-stream-rx/README.md](../gar-stream-rx/README.md)
の直接 `rtpjpegpay` パイプライン（デコード/エンコード無し）に固定した方が
CPU 負荷は下がる。

## 1. 配線

Raspberry Pi 5 の BCM GPIO 番号をそのまま使う（Luckfox と違い `luckfox-config`
のようなピンマルチプレクサは不要）。デフォルトは:

| KY-040 | GPIO (BCM) |
|---|---|
| CLK | GPIO17 |
| DT  | GPIO27 |
| SW  | GPIO22 |
| +   | 3.3V |
| GND | GND |

配線が違う場合は `camera_tx.py` の `CONFIG` を書き換える。KY-040 ボードは
`CLK`/`DT`/`SW` にプルアップが無いことが多いので、読み取りがガタつく場合は
3.3V に 10kΩ でプルアップする（[gar-stream-rx/README.md](../gar-stream-rx/README.md)
の同じ注記を参照）。

カメラは USB2.0 ポートへ直結する（本製品は Android/iOS 向け OTG 対応だが、
Pi 5 では通常の USB UVC カメラとして `/dev/video0` に見える）。

### オプション: ローカルプレビュー用 ILI9341

`CONFIG["local_display"] = True` にする場合は、[gar-stream-rx/README.md](../gar-stream-rx/README.md)
の「1. 配線」と同じ要領で ILI9341 を Pi 5 の SPI0 に配線する
（`VCC`→3.3V, `GND`→GND, `CS`→SPI0 CE0, `RESET`→任意の GPIO, `DC`→任意の
GPIO, `SDI(MOSI)`→SPI0 MOSI, `SCK`→SPI0 SCLK, `LED`→3.3V）。Raspberry Pi は
`raspi-config` で SPI を有効化するだけで済み、Luckfox のようなピン
マルチプレクサ設定は不要。DC/RESET は空いている GPIO を好きに選び、
`CONFIG["dc_gpio"]`/`CONFIG["rst_gpio"]` に書く。

## 2. CONFIG を埋める

`camera_tx.py` 先頭の `CONFIG` を編集する。**`rx_host` は必須**（未設定だと
起動時にエラーで止まる）。ローカルプレビューを使わないなら
`local_display`/`dc_gpio`/`rst_gpio` はそのままで良い。

```python
CONFIG = {
    "enc_clk_gpio": 17,
    "enc_dt_gpio": 27,
    "enc_sw_gpio": 22,
    "camera_device": "/dev/video0",
    "native_width": 2048,
    "native_height": 1536,
    "native_fps": 15,
    "rx_host": "192.168.1.50",  # <- gar-stream-rx (Lyra Plus) の IP
    "rx_port": 5600,
    "jpeg_quality": 85,

    # TX に直結した ILI9341 でローカルプレビューを出す場合は True にして
    # dc_gpio/rst_gpio も埋める（配線は README「1. 配線」参照）。
    "local_display": False,
    "spi_bus": 0,
    "spi_device": 0,
    "spi_max_hz": 24_000_000,
    "dc_gpio": None,
    "rst_gpio": None,
}
```

## 3. Profile とオンスクリーンメニュー

```python
PROFILES = (
    ("Low latency", 320, 240),
    ("Standard", 640, 480),
    ("High quality", 1024, 768),
    ("Maximum", 2048, 1536),
)
FIXED_FPS = 15
```

いずれも 4:3 に揃えている。RX 側の ILI9341 パネルが 320×240（4:3）なので、
レターボックス無しで表示できる。既定値の Standard（640×480）はパネル解像度
のちょうど2倍で、SPI 帯域にも余裕がある。映像転送の安定性を保つためFPSは
15fps固定であり、Profileを変えてもFPSは変化しない。

メニューを開いている間は、OverlayがOFFでも操作内容が送信映像に表示される。
決定して閉じた後はOverlay設定がそのまま反映される。MirrorとRotateは送信側
で適用されるため、RXの表示およびネットワーク上の映像に同じ結果が届く。

## 4. 実行

GStreamer 本体と PyGObject バインディングは pip ではなくシステムパッケージで
入れる（Raspberry Pi OS は Debian ベースなので、Luckfox の Buildroot と違い
クロスコンパイル無しで素直に apt から入る）:

```bash
sudo apt install python3-gi gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
pip install -r requirements.txt   # python-periphery (+ spidev, local_display用)
python3 camera_tx.py
```

起動ログに現在のProfileと `WxH@15fps -> host:port` が出る。KY-040を押して
メニューを開き、回して項目を選んでから押すと編集に入る。もう一度押すと値を
確定してメニューを閉じる。Profileの変更はネットワーク分岐の`capsfilter`を
ライブ更新するため、カメラ入力を再オープンしない（数フレーム分の乱れはあり得る）。

`Ctrl-C` で GStreamer パイプラインと KY-040 のスレッドを両方止める。

## トラブルシューティング

- **`ModuleNotFoundError: No module named 'gi'`**: PyGObject はシステム
  パッケージ（`sudo apt install python3-gi`）で入れる。pip の `PyGObject`
  はビルドが面倒で非推奨（gar-stream-rx の README と同じ注意）。
- **`gst-launch-1.0`/GStreamer 系コマンドが無い**: `sudo apt install
  gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad`
  （jpegenc/jpegdec/rtpjpegpay は plugins-good、環境によっては
  plugins-bad 側にあることも）。
- **`/dev/video0` が無い / Permission denied**: `ls -l /dev/video*` で確認し、
  ユーザーを `video` グループに入れる（`sudo usermod -aG video $USER` 後に
  再ログイン）。
- **起動直後にすぐ終了する**: カメラが本当に `2048x1536@15fps` の MJPEG を
  出しているか `v4l2-ctl --list-formats-ext -d /dev/video0` で確認する。
  ネイティブモードがこれと違う場合は `CONFIG["native_width"/"native_height"/
  "native_fps"]` を実際の値に合わせる。
- **KY-040 を回してもサイズが変わらない**: 配線/GPIO 番号を確認
  （[gar-stream-rx/README.md](../gar-stream-rx/README.md) と同じ症状・対策）。
- **RX 側に映像が来ない**: `rx_host`/`rx_port` が gar-stream-rx 側の
  `udpsrc port=5600` と一致しているか、同一サブネットで UDP がブロックされて
  いないか確認する。
- **ローカルプレビューが真っ暗/映らない**: `local_display=True` にした際に
  `dc_gpio`/`rst_gpio` を埋め忘れていないか、配線が
  [gar-stream-rx/README.md](../gar-stream-rx/README.md) と同じ極性/結線に
  なっているか確認する（ILI9341 側のトラブルシューティングもそちらを参照）。

## EC2 でのシミュレーション

[gar-stream-rx](../gar-stream-rx/README.md) と同様、こちらも本物のボードが
無くても `gar-tools` の linux-device ランタイム（EC2 Graviton 上で `/dev/*`
互換を提供する仕組み）で大部分を検証できる。

- **KY-040（GPIO）**: gar-stream-rx 用に追加した
  [gar-tools/targets/linux-device/runtime/web-bridge](../gar-tools/targets/linux-device/runtime/web-bridge/bridge.py)
  の rotary シミュレーション（`ROTARY_CLK_LINE`/`ROTARY_DT_LINE`/
  `ROTARY_SW_LINE` の gpio-sim 制御、Virtual Hardware Panel の「◀ ● ▶」
  ダイヤル）がそのまま使える。gar-stream-tx と gar-stream-rx は別々の EC2
  インスタンス（別ターゲット）で動かす想定なので、同じ line 番号を使っても
  衝突しない。
- **カメラ（`/dev/video0`）**: 本物の UVC カメラの CUSE シミュレーションは
  ここでは実装していない。V4L2 の CUSE 化は `gar-tools/targets/luckfox-rv1106/
  docs/03_CAMERA_CUSE_ROADMAP.md` に別途ロードマップがある大きめのタスクで、
  このプロジェクト単体で先取り実装するのは過剰。代わりに、同じくロードマップ
  で「transitional fallback」と位置づけられている **`v4l2loopback`** を使う:

  ```bash
  sudo modprobe v4l2loopback video_nr=0 card_label="OV3660 sim" exclusive_caps=1
  # 別プロセスでダミー映像を /dev/video0 に流し込む（テストパターン）
  gst-launch-1.0 -v videotestsrc pattern=smpte is-live=true \
    ! video/x-raw,width=2048,height=1536,framerate=15/1 \
    ! videoconvert ! jpegenc ! v4l2sink device=/dev/video0
  ```

  こうすると `camera_tx.py` 自体は一切変更せずに（`/dev/video0` を開いて
  `image/jpeg,width=2048,height=1536,framerate=15/1` を要求するだけ）、KY-040
  によるサイズ/レート切り替えと `out_caps` のライブ差し替えロジックを EC2 上で
  検証できる。実際のカメラ固有の ISP/UVC ディスクリプタ挙動までは検証できない
  点に注意（それは実機 Pi 5 + 実カメラでの確認が必要）。
- **ローカルプレビュー（`local_display=True`）を EC2 で検証する場合**:
  gar-stream-rx 用に追加した
  [gar-tools/targets/linux-device/runtime/ili9341-stub](../gar-tools/targets/linux-device/runtime/ili9341-stub/README.md)
  の CUSE SPI スタブ（`/dev/spidev0.0` + DC ピン問い合わせ + フレームバッファを
  Virtual Hardware Panel に表示）がそのまま使える。ILI9341 のコマンド/データ
  プロトコルは gar-stream-rx と共通なので、追加のシミュレータ実装は不要。
