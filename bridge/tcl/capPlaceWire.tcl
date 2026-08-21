# Place a wire between two pins on the same schematic page, at database level.
# Uses DboPage_NewWireScalar {page status startCPoint endCPoint} -- proven
# 2026-08-17 to create real wires that the page's own iterator reports.
# placeWireBetweenPins {refdesA pinA refdesB pinB apply}
#
# apply defaults to 0. The preview path returns the resolved page and absolute
# endpoints without modifying the design. Requiring an explicit true value
# makes accidental tool calls safe even when the MCP server has write access.
proc ::capBridge::placeWireBetweenPins { pList } {
    set refA [lindex $pList 0]
    set pinA [lindex $pList 1]
    set refB [lindex $pList 2]
    set pinB [lindex $pList 3]
    set apply [string tolower [string trim [lindex $pList 4]]]
    if { $refA eq "" || $pinA eq "" || $refB eq "" || $pinB eq "" } {
        return [::capBridge::_err "usage: placeWireBetweenPins {refdesA pinA refdesB pinB apply}"]
    }
    set doApply [expr {$apply in {1 true yes on}}]

    set st [DboState]
    set instA [::capBridge::_findPart $refA $st]
    set instB [::capBridge::_findPart $refB $st]
    if { $instA eq "" } { return [::capBridge::_err "no part '$refA'"] }
    if { $instB eq "" } { return [::capBridge::_err "no part '$refB'"] }

    set A [$instA GetPinByPinNumber [DboTclHelper_sMakeCString $pinA] $st]
    set B [$instB GetPinByPinNumber [DboTclHelper_sMakeCString $pinB] $st]
    if { $A eq "NULL" } { return [::capBridge::_err "pin '$refA.$pinA' not found"] }
    if { $B eq "NULL" } { return [::capBridge::_err "pin '$refB.$pinB' not found"] }
    set ax [DboPortInst_sGetHotSpotX $A $st]; set ay [DboPortInst_sGetHotSpotY $A $st]
    set bx [DboPortInst_sGetHotSpotX $B $st]; set by [DboPortInst_sGetHotSpotY $B $st]

    # Locate both owning pages. Wiring across pages is invalid and must fail
    # before any constructor is called.
    set d [::pcbWorkflows::_getActiveDesign]
    set pageA NULL; set pageB NULL; set pageNameA ""; set pageNameB ""
    set vIter [$d NewViewsIter $st $::IterDefs_SCHEMATICS]
    set v [$vIter NextView $st]
    while { $v ne "NULL" && ($pageA eq "NULL" || $pageB eq "NULL") } {
        set sch [DboViewToDboSchematic $v]
        set pIter [$sch NewPagesIter $st]
        set pg [$pIter NextPage $st]
        while { $pg ne "NULL" } {
            set iIter [$pg NewPartInstsIter $st]
            set i [$iIter NextPartInst $st]
            while { $i ne "NULL" } {
                set rd [::capBridge::_refDes $i $st]
                if { $rd eq $refA } { set pageA $pg; set pageNameA [::pcbWorkflows::_getPageLocation $pg] }
                if { $rd eq $refB } { set pageB $pg; set pageNameB [::pcbWorkflows::_getPageLocation $pg] }
                set i [$iIter NextPartInst $st]
            }
            ::pcbWorkflows::_deleteIter $iIter
            set pg [$pIter NextPage $st]
        }
        ::pcbWorkflows::_deleteIter $pIter
        set v [$vIter NextView $st]
    }
    ::pcbWorkflows::_deleteIter $vIter
    if { $pageA eq "NULL" } { return [::capBridge::_err "page for '$refA' not found"] }
    if { $pageB eq "NULL" } { return [::capBridge::_err "page for '$refB' not found"] }
    if { $pageNameA ne $pageNameB } {
        return [::capBridge::_err "cannot wire across pages: '$refA' is on '$pageNameA', '$refB' is on '$pageNameB'"]
    }

    if { !$doApply } {
        return [list OK preview page $pageNameA from "$refA.$pinA" "$ax,$ay" to "$refB.$pinB" "$bx,$by"]
    }

    set w [DboPage_NewWireScalar $pageA $st \
             [DboTclHelper_sMakeCPoint $ax $ay] [DboTclHelper_sMakeCPoint $bx $by]]
    if { $w eq "NULL" || $w eq "" } {
        return [::capBridge::_err "DboPage_NewWireScalar returned no wire"]
    }
    return [list OK created wire $w page $pageNameA from "$refA.$pinA" "$ax,$ay" to "$refB.$pinB" "$bx,$by"]
}
