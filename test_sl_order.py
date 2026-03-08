"""
test_sl_order.py — Teste si STOP_LOSS_LIMIT est supporté sur Binance Testnet.
Lance ce script localement pour vérifier avant de déployer.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, ".")
from order_executor import OrderExecutor

def test_sl_order():
    print("🔧 Connexion Binance Testnet...")
    ex = OrderExecutor()

    symbol = "BTC/USDT"
    print(f"\n📊 Récupération prix actuel {symbol}...")
    ticker = ex.exchange.fetch_ticker(symbol)
    price  = ticker["last"]
    print(f"   Prix actuel : {price:,.2f} USDT")

    # Test d'un micro ordre SL (0.001 BTC) à -3% sous le prix
    sl_price  = round(price * 0.97, 2)
    sl_limit  = round(sl_price * 0.999, 2)
    amount    = 0.001  # Minimum Binance

    print(f"\n🔒 Test STOP_LOSS_LIMIT sell :")
    print(f"   Stop  : {sl_price:,.2f}")
    print(f"   Limit : {sl_limit:,.2f}")
    print(f"   Qté   : {amount} BTC\n")

    try:
        order = ex.exchange.create_order(
            symbol=symbol,
            type="STOP_LOSS_LIMIT",
            side="sell",
            amount=amount,
            price=sl_limit,
            params={"stopPrice": sl_price, "timeInForce": "GTC"}
        )
        order_id = order["id"]
        print(f"✅ STOP_LOSS_LIMIT SUPPORTÉ — ID: {order_id}")

        # Annule l'ordre de test immédiatement
        ex.exchange.cancel_order(order_id, symbol)
        print(f"🗑️  Ordre de test annulé (ID: {order_id})")
        print("\n✅ RÉSULTAT : Les vrais ordres SL fonctionnent sur Binance Testnet !")
        return True

    except Exception as e:
        print(f"❌ STOP_LOSS_LIMIT non supporté : {e}")
        print("\n⚠️  Le bot passera en mode surveillance logicielle (fallback automatique).")
        print("   Impact : le SL ne survivra pas à un crash Railway — surveillance active.")
        return False

if __name__ == "__main__":
    test_sl_order()
