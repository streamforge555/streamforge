# Streamforge Tiny2 User Camera Control for StripChat

StripChatの公開コメントから **OBSBOT Tiny 2** を操作する実験用ツールです。

配信者が許可したタイミングで、対象リクエストを送ったユーザー本人だけが、コメント欄からTiny2のパン・チルト・ズームを操作できます。

> TEST v0.4.6 / Windows 10・11向け
>
> StripChat、OBSBOTの公式ツールではありません。Streamforgeによる実験版です。

## できること

| コメント | 動作 |
| --- | --- |
| `c←` | 左 |
| `c→` | 右 |
| `c↑` | 上 |
| `c↓` | 下 |
| `c+` | ズームイン |
| `c-` | ズームアウト |

小文字ASCII `c` の完全一致のみ反応します。

操作が通るのは、次の条件をすべて満たした場合だけです。

- Windows側Tiny2 controllerが起動している
- Tiny2操作受付がON
- Streamforgeリク管理のNOWが `🎥 Tiny2カメラ操作権`
- コメント送信者がそのリクエスト本人
- StripChatの数値userIdを確認できる
- コマンドが完全一致
- 連打防止クールダウンを通過

## 処理の流れ

```text
StripChat 公開コメント
        ↓
Tampermonkey userscript
        ↓
NOW / リク主 / userId 判定
        ↓
127.0.0.1:8765
        ↓
Python controller
        ↓
OBSBOT Center Global Hotkey
        ↓
OBSBOT Tiny 2
```

StripChatのAPIからカメラを操作するものではありません。カメラ操作処理はローカルPC内で完結します。

## 必要なもの

- Windows 10 / 11
- Python 3
- OBS Studio
- OBSBOT Center
- OBSBOT Tiny 2
- Chrome系ブラウザ
- Tampermonkey
- Streamforgeリクエスト管理

## 初回セットアップ

1. `Streamforge_Tiny2_UserControl_v0.4.6_TEST.user.js` をTampermonkeyへ登録して有効化
2. `setup_autostart_once.bat` を1回実行
3. `http://127.0.0.1:8765/test` を開いて動作確認
4. OBS Studio / OBSBOT Center / Streamforgeリク管理を起動

セットアップ後はWindowsログイン時にcontrollerがバックグラウンド起動し、操作受付OFFで待機します。

詳しいScene構成、ホットキー、実地テスト手順は `Streamforge_Tiny2_UserControl_v0.4.6_説明書.txt` を参照してください。

## OBS Scene構成

基準版では以下を使用します。

- Scene 1 = Tiny3 / Tiny2操作OFF
- Scene 2 = Tiny2 / 操作ガイドなし / Tiny2操作OFF
- Scene 3 = MEET / Tiny2操作OFF
- Scene 4 = Tiny2 + 操作ガイド / Tiny2操作ON

上段 `1 / 2 / 3 / 4` を使用します。テンキーは対象外です。

## OBSBOT Center Global Hotkey

- 左: `Ctrl + Alt + ←`
- 右: `Ctrl + Alt + →`
- 上: `Ctrl + Alt + ↑`
- 下: `Ctrl + Alt + ↓`
- ズームイン: `Ctrl + Alt + I`
- ズームアウト: `Ctrl + Alt + O`

同じホットキーを他アプリへ重複設定しないでください。

## localhostについて

controllerは `127.0.0.1:8765` にのみバインドします。

LANやインターネットへ公開する前提ではありません。ポート転送や外部公開はしないでください。

## 関連note

開発経緯と、実際の配信では採用しなかった理由はこちら。

**視聴者がコメントでカメラを動かせるようにしてみた。でも没になった話**

https://note.com/steamforge555/n/n798ffa1c2202

## ファイル

- `Streamforge_Tiny2_UserControl_v0.4.6_TEST.user.js` — StripChat側Tampermonkey
- `tiny2_camera_v0.4.6.py` — Windows側controller
- `tiny2_config.json` — ホットキー等の設定
- `setup_autostart_once.bat` / `setup_autostart.ps1` — 初回セットアップ
- `start_tiny2_test.bat` — 手動起動確認用
- `selftest.bat` — 簡易セルフテスト
- `Streamforge_Tiny2_UserControl_v0.4.6_説明書.txt` — 詳細手順

## 注意

StripChatやOBSBOT側のDOM・ソフトウェア仕様・ホットキー仕様変更により動作しなくなる場合があります。

実験版のため、実配信へ入れる前に必ずローカル環境で動作確認してください。