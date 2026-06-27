#!/usr/bin/env python3
"""
Throwaway unit test for _parse_form4_xml fix.
Tests three synthetic Form 4 cases: mixed, pure exercise, plain sale.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from collectors.m7_insider import _parse_form4_xml
import json


def test_mixed_form4():
    """
    Mixed: one M (exercise 304M @ 44.13) + one S (sale 17.5M @ 430).
    Expected: action="Sell", value_usd ≈ 17.5M*430 (not 14.2B),
              market_shares=17500000, nonmarket_shares=304000000, tx_codes=["M","S"]
    """
    xml = """<?xml version="1.0"?>
<ownershipDocument>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName>Musk, Elon</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>0</isOfficer>
            <isTenPercentOwner>1</isTenPercentOwner>
            <isOther>0</isOther>
            <officerTitle>CEO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-06-16</value></transactionDate>
            <transactionCoding>
                <transactionCode>M</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>304000000</value></transactionShares>
                <transactionPricePerShare><value>44.13</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-06-16</value></transactionDate>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>17500000</value></transactionShares>
                <transactionPricePerShare><value>430.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""
    result = _parse_form4_xml(xml, "0001104659-26-075213")
    print("\n=== TEST A: Mixed (M exercise + S sale) ===")
    print(f"action: {result['action']} (expect: Sell)")
    print(f"headline shares: {result['shares']} (expect: 17500000)")
    print(f"headline value_usd: {result['value_usd']:.2f} (expect: ~7,525,000,000)")
    print(f"headline price_usd: {result['price_usd']} (expect: 430.0)")
    print(f"market_shares: {result['market_shares']} (expect: 17500000)")
    print(f"nonmarket_shares: {result['nonmarket_shares']} (expect: 304000000)")
    print(f"tx_codes: {result['tx_codes']} (expect: ['M', 'S'])")

    assert result['action'] == 'Sell', f"Expected action='Sell', got {result['action']}"
    assert result['shares'] == 17500000, f"Expected 17500000 shares, got {result['shares']}"
    assert abs(result['value_usd'] - 7525000000.0) < 1.0, f"Expected ~7.525B, got {result['value_usd']}"
    assert result['price_usd'] == 430.0, f"Expected 430.0, got {result['price_usd']}"
    assert result['market_shares'] == 17500000, f"Expected market_shares=17500000, got {result['market_shares']}"
    assert result['nonmarket_shares'] == 304000000, f"Expected nonmarket_shares=304000000, got {result['nonmarket_shares']}"
    assert result['tx_codes'] == ['M', 'S'], f"Expected ['M', 'S'], got {result['tx_codes']}"
    print("✓ PASS")


def test_pure_exercise():
    """
    Pure exercise: single M (exercise 100M @ 44.13).
    Expected: action="Exercise", value_usd=0.0, shares=0.0, market_shares=0, nonmarket_shares=100M
    """
    xml = """<?xml version="1.0"?>
<ownershipDocument>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName>Musk, Elon</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>0</isOfficer>
            <isTenPercentOwner>1</isTenPercentOwner>
            <isOther>0</isOther>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-06-16</value></transactionDate>
            <transactionCoding>
                <transactionCode>M</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>100000000</value></transactionShares>
                <transactionPricePerShare><value>44.13</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""
    result = _parse_form4_xml(xml, "0001104659-26-999999")
    print("\n=== TEST B: Pure Exercise (M only) ===")
    print(f"action: {result['action']} (expect: Exercise)")
    print(f"headline value_usd: {result['value_usd']} (expect: 0.0)")
    print(f"headline shares: {result['shares']} (expect: 0.0)")
    print(f"nonmarket_shares: {result['nonmarket_shares']} (expect: 100000000)")
    print(f"nonmarket_value_usd: {result['nonmarket_value_usd']:.2f} (expect: ~4,413,000,000)")
    print(f"tx_codes: {result['tx_codes']} (expect: ['M'])")

    assert result['action'] == 'Exercise', f"Expected action='Exercise', got {result['action']}"
    assert result['value_usd'] == 0.0, f"Expected value_usd=0.0, got {result['value_usd']}"
    assert result['shares'] == 0.0, f"Expected shares=0.0, got {result['shares']}"
    assert result['nonmarket_shares'] == 100000000, f"Expected nonmarket_shares=100000000, got {result['nonmarket_shares']}"
    assert abs(result['nonmarket_value_usd'] - 4413000000.0) < 1.0, f"Expected ~4.4B, got {result['nonmarket_value_usd']}"
    assert result['tx_codes'] == ['M'], f"Expected ['M'], got {result['tx_codes']}"
    print("✓ PASS")


def test_plain_sale():
    """
    Plain sale: single S (sale 100k @ 300).
    Expected: action="Sell", value_usd=30,000,000, shares=100000, price_usd=300.0
    """
    xml = """<?xml version="1.0"?>
<ownershipDocument>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerName>Test, Insider</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>0</isDirector>
            <isOfficer>1</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
            <isOther>0</isOther>
        </reportingOwnerRelationship>
    </reportingOwner>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-06-16</value></transactionDate>
            <transactionCoding>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>100000</value></transactionShares>
                <transactionPricePerShare><value>300.00</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""
    result = _parse_form4_xml(xml, "0001104659-26-888888")
    print("\n=== TEST C: Plain Sale (S only) ===")
    print(f"action: {result['action']} (expect: Sell)")
    print(f"headline shares: {result['shares']} (expect: 100000)")
    print(f"headline value_usd: {result['value_usd']} (expect: 30000000)")
    print(f"headline price_usd: {result['price_usd']} (expect: 300.0)")
    print(f"market_shares: {result['market_shares']} (expect: 100000)")
    print(f"nonmarket_shares: {result['nonmarket_shares']} (expect: 0.0)")
    print(f"tx_codes: {result['tx_codes']} (expect: ['S'])")

    assert result['action'] == 'Sell', f"Expected action='Sell', got {result['action']}"
    assert result['shares'] == 100000, f"Expected 100000 shares, got {result['shares']}"
    assert result['value_usd'] == 30000000, f"Expected 30,000,000, got {result['value_usd']}"
    assert result['price_usd'] == 300.0, f"Expected 300.0, got {result['price_usd']}"
    assert result['market_shares'] == 100000, f"Expected market_shares=100000, got {result['market_shares']}"
    assert result['nonmarket_shares'] == 0.0, f"Expected nonmarket_shares=0.0, got {result['nonmarket_shares']}"
    assert result['tx_codes'] == ['S'], f"Expected ['S'], got {result['tx_codes']}"
    print("✓ PASS")


if __name__ == "__main__":
    try:
        test_mixed_form4()
        test_pure_exercise()
        test_plain_sale()
        print("\n" + "="*50)
        print("ALL TESTS PASSED ✓")
        print("="*50)
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
