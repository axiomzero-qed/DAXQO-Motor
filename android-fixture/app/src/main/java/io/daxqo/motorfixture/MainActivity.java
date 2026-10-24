package io.daxqo.motorfixture;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.Locale;

public final class MainActivity extends Activity {
    private static final int TAB_HOME = 0;
    private static final int TAB_PRODUCTS = 1;
    private static final int TAB_FAVORITES = 2;
    private static final int TAB_PROFILE = 3;
    private static final int PAGE_DETAIL = 4;
    private static final int PAGE_NEW_FEATURE = 5;
    private static final int PAGE_LOOP_LIST = 6;
    private static final int PAGE_EXTRA_INFO = 7;
    private static final int ID_TAB_HOME = R.id.motor_tab_home;
    private static final int ID_TAB_PRODUCTS = R.id.motor_tab_products;
    private static final int ID_TAB_FAVORITES = R.id.motor_tab_favorites;
    private static final int ID_TAB_PROFILE = R.id.motor_tab_profile;
    private static final int ID_TAB_RECOMMENDED = R.id.motor_tab_recommended;
    private static final int ID_TAB_NEW = R.id.motor_tab_new;
    private static final int ID_PROFILE_EXTRA_ENTRY = R.id.motor_profile_extra_entry;

    private int page = TAB_HOME;
    private int returnPage = TAB_HOME;
    private int selectedProduct = 1;
    private int productSubTab = 0;
    private String selectedProductName = "商品 01";
    private int deleteAttempts = 0;
    private int productsScrollY = 0;
    private boolean delayedLoaded = false;
    private boolean loadingDelayed = false;
    private boolean extraEntryVisible = false;
    private int loopCount = 0;
    private boolean loopAppending = false;
    private LinearLayout root;
    private LinearLayout body;
    private LinearLayout productRows;

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        if (state != null) {
            page = state.getInt("page", TAB_HOME);
            returnPage = state.getInt("returnPage", TAB_HOME);
            selectedProduct = state.getInt("product", 1);
            productSubTab = state.getInt("productSubTab", 0);
            selectedProductName = state.getString("productName", "商品 01");
            deleteAttempts = state.getInt("deleteAttempts", 0);
            productsScrollY = state.getInt("productsScrollY", 0);
            delayedLoaded = state.getBoolean("delayedLoaded", false);
            extraEntryVisible = state.getBoolean("extraEntryVisible", false);
            loopCount = state.getInt("loopCount", 0);
        }
        render();
    }

    @Override protected void onSaveInstanceState(Bundle out) {
        out.putInt("page", page);
        out.putInt("returnPage", returnPage);
        out.putInt("product", selectedProduct);
        out.putInt("productSubTab", productSubTab);
        out.putString("productName", selectedProductName);
        out.putInt("deleteAttempts", deleteAttempts);
        out.putInt("productsScrollY", productsScrollY);
        out.putBoolean("delayedLoaded", delayedLoaded);
        out.putBoolean("extraEntryVisible", extraEntryVisible);
        out.putInt("loopCount", loopCount);
        super.onSaveInstanceState(out);
    }

    @Override public void onBackPressed() {
        if (page == PAGE_DETAIL || page == PAGE_NEW_FEATURE || page == PAGE_LOOP_LIST || page == PAGE_EXTRA_INFO) page = returnPage;
        else if (page != TAB_HOME) page = TAB_HOME;
        else { super.onBackPressed(); return; }
        render();
    }

    private void render() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(247, 248, 250));
        setContentView(root);

        TextView title = new TextView(this);
        title.setText("DAXQO Motor Fixture\n" + pageTitle());
        title.setTextSize(22);
        title.setTextColor(Color.rgb(28, 43, 62));
        title.setPadding(dp(18), dp(14), dp(18), dp(12));
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        body = new LinearLayout(this);
        body.setId(R.id.motor_page_content);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding(dp(16), dp(4), dp(16), dp(8));
        root.addView(body, new LinearLayout.LayoutParams(-1, 0, 1f));
        if (page == TAB_HOME) renderHome();
        else if (page == TAB_PRODUCTS) renderProducts();
        else if (page == TAB_FAVORITES) renderFavorites();
        else if (page == TAB_PROFILE) renderProfile();
        else if (page == PAGE_DETAIL) renderDetail();
        else if (page == PAGE_NEW_FEATURE) renderNewFeature();
        else if (page == PAGE_LOOP_LIST) renderLoopList();
        else renderExtraInfo();
        renderTabs();
    }

    private String pageTitle() {
        switch (page) {
            case TAB_PRODUCTS: return "商品";
            case TAB_FAVORITES: return "收藏";
            case TAB_PROFILE: return "我的";
            case PAGE_DETAIL: return "商品详情";
            case PAGE_NEW_FEATURE: return "新功能";
            case PAGE_LOOP_LIST: return "循环列表测试模式";
            case PAGE_EXTRA_INFO: return "附加说明";
            default: return "首页";
        }
    }

    private void renderTabs() {
        LinearLayout nav = new LinearLayout(this);
        nav.setId(R.id.motor_bottom_navigation);
        nav.setContentDescription("Bottom navigation");
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setBackgroundColor(Color.WHITE);
        String[] labels = {"首页", "商品", "收藏", "我的"};
        int[] ids = {ID_TAB_HOME, ID_TAB_PRODUCTS, ID_TAB_FAVORITES, ID_TAB_PROFILE};
        for (int i = 0; i < labels.length; i++) {
            final int destination = i;
            Button tab = new Button(this);
            tab.setId(ids[i]);
            tab.setText(labels[i]);
            tab.setAllCaps(false);
            tab.setSelected(page == i);
            tab.setOnClickListener(v -> {
                saveScroll();
                page = destination;
                render();
            });
            nav.addView(tab, new LinearLayout.LayoutParams(0, dp(54), 1f));
        }
        root.addView(nav, new LinearLayout.LayoutParams(-1, dp(54)));
    }

    private void renderHome() {
        info("欢迎来到 DAXQO Motor");
        info("在商品页浏览虚构商品，在收藏页查看跨标签目标。");
        body.addView(button("去逛商品", () -> { page = TAB_PRODUCTS; render(); }), match());
    }

    private void renderProducts() {
        HorizontalScrollView rail = new HorizontalScrollView(this);
        rail.setHorizontalScrollBarEnabled(false);
        LinearLayout railItems = new LinearLayout(this);
        railItems.setOrientation(LinearLayout.HORIZONTAL);
        Button recommended = button("推荐", () -> selectProductTab(0));
        recommended.setId(ID_TAB_RECOMMENDED);
        recommended.setSelected(productSubTab == 0);
        railItems.addView(recommended, new LinearLayout.LayoutParams(dp(120), dp(48)));
        Button newest = button("新品", () -> selectProductTab(1));
        newest.setId(ID_TAB_NEW);
        newest.setSelected(productSubTab == 1);
        railItems.addView(newest, new LinearLayout.LayoutParams(dp(120), dp(48)));
        rail.addView(railItems);
        body.addView(rail, new LinearLayout.LayoutParams(-1, dp(50)));

        Button load = button(loadingDelayed ? "加载中…" : delayedLoaded ? "延迟商品已加载" : "加载更多商品（延迟）", () -> {
            if (loadingDelayed || delayedLoaded) return;
            loadingDelayed = true;
            render();
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                loadingDelayed = false;
                delayedLoaded = true;
                if (page == TAB_PRODUCTS) render();
            }, 500);
        });
        load.setEnabled(!loadingDelayed && !delayedLoaded);
        body.addView(load, match());

        body.addView(button("查看循环列表（测试模式）", () -> {
            saveScroll();
            returnPage = TAB_PRODUCTS;
            page = PAGE_LOOP_LIST;
            render();
        }), match());

        ScrollView scroll = new ScrollView(this);
        scroll.setId(R.id.motor_products_scroll);
        scroll.setContentDescription("商品列表，可滚动");
        productRows = new LinearLayout(this);
        productRows.setOrientation(LinearLayout.VERTICAL);
        for (int i = 1; i <= 24; i++) addProductRow(i, productName(i));
        if (delayedLoaded) for (int i = 25; i <= 32; i++) addProductRow(i, "延迟商品 " + String.format(Locale.ROOT, "%02d", i));
        scroll.addView(productRows);
        body.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1f));
        scroll.post(() -> scroll.scrollTo(0, productsScrollY));
    }

    private void selectProductTab(int tab) {
        saveScroll();
        productSubTab = tab;
        productsScrollY = 0;
        render();
    }

    private String productName(int number) {
        return String.format(Locale.ROOT, productSubTab == 0 ? "商品 %02d" : "新品 %02d", number);
    }

    private void addProductRow(int number, String name) {
        TextView row = new TextView(this);
        row.setText(name);
        row.setTextSize(18);
        row.setTextColor(Color.rgb(35, 49, 66));
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(14), 0, dp(10), 0);
        row.setBackgroundColor(number % 2 == 0 ? Color.WHITE : Color.rgb(239, 243, 247));
        row.setContentDescription(name + "，查看详情");
        row.setClickable(true);
        row.setFocusable(true);
        row.setOnClickListener(v -> {
            saveScroll();
            selectedProduct = number;
            selectedProductName = name;
            returnPage = TAB_PRODUCTS;
            page = PAGE_DETAIL;
            render();
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, dp(58));
        lp.bottomMargin = dp(2);
        productRows.addView(row, lp);
    }

    private void renderFavorites() {
        info("跨标签目标：商品 19");
        body.addView(button("打开商品 19", () -> {
            selectedProduct = 19;
            returnPage = TAB_FAVORITES;
            page = PAGE_DETAIL;
            render();
        }), match());
    }

    private void renderProfile() {
        info("我的 DAXQO Motor");
        body.addView(button("显示提示", () -> new AlertDialog.Builder(this)
                .setTitle("提示").setMessage("这是测试提示弹窗。")
                .setPositiveButton("关闭", (dialog, which) -> dialog.dismiss()).show()), match());
        info("危险动作触发次数：" + deleteAttempts);
        body.addView(button("删除全部", () -> { deleteAttempts++; render(); }), match());
        body.addView(button("打开新功能页", () -> {
            returnPage = TAB_PROFILE;
            page = PAGE_NEW_FEATURE;
            render();
        }), match());
        body.addView(button("显示附加入口", () -> {
            extraEntryVisible = true;
            render();
        }), match());
        if (extraEntryVisible) {
            Button extra = button("查看附加说明", () -> {
                returnPage = TAB_PROFILE;
                page = PAGE_EXTRA_INFO;
                render();
            });
            extra.setId(ID_PROFILE_EXTRA_ENTRY);
            body.addView(extra, match());
        }
    }

    private void renderDetail() {
        info(String.format(Locale.ROOT, "商品 %02d", selectedProduct));
        info("这是用于自动化验收的虚构商品。");
        body.addView(button("返回", () -> { page = returnPage; render(); }), match());
    }

    private void renderNewFeature() {
        info("这是仅用于测试的新增页面结构。");
        info("返回操作会恢复上一页。");
    }

    private void renderExtraInfo() {
        info("附加说明：这是无副作用的本地测试页面。");
        body.addView(button("返回我的", () -> { page = TAB_PROFILE; render(); }), match());
    }

    private void renderLoopList() {
        info("自动懒加载仅在此明确测试模式中启用。");
        ScrollView scroll = new ScrollView(this);
        scroll.setContentDescription("循环列表测试模式，可滚动加载");
        LinearLayout rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        int initial = loopCount == 0 ? 20 : loopCount;
        for (int i = 1; i <= initial; i++) addLoopRow(rows, i);
        loopCount = Math.max(loopCount, initial);
        scroll.addView(rows);
        scroll.setOnScrollChangeListener((View view, int x, int y, int oldX, int oldY) -> {
            if (!loopAppending && y + scroll.getHeight() >= rows.getHeight() - dp(100)) {
                loopAppending = true;
                int start = loopCount + 1;
                for (int i = start; i < start + 20; i++) addLoopRow(rows, i);
                loopCount += 20;
                loopAppending = false;
            }
        });
        body.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1f));
    }

    private void addLoopRow(LinearLayout rows, int number) {
        TextView row = new TextView(this);
        row.setText(String.format(Locale.ROOT, "循环项目 %04d", number));
        row.setTextSize(18);
        row.setTextColor(Color.rgb(35, 49, 66));
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(14), 0, dp(10), 0);
        row.setBackgroundColor(number % 2 == 0 ? Color.WHITE : Color.rgb(239, 243, 247));
        rows.addView(row, new LinearLayout.LayoutParams(-1, dp(58)));
    }

    private void saveScroll() {
        View scroll = findViewById(R.id.motor_products_scroll);
        if (scroll instanceof ScrollView) productsScrollY = scroll.getScrollY();
    }

    private void info(String value) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextSize(18);
        text.setTextColor(Color.rgb(35, 49, 66));
        text.setPadding(dp(4), dp(12), dp(4), dp(12));
        body.addView(text, new LinearLayout.LayoutParams(-1, -2));
    }

    private Button button(String text, Runnable action) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setOnClickListener(v -> action.run());
        return button;
    }

    private LinearLayout.LayoutParams match() { return new LinearLayout.LayoutParams(-1, -2); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
}
