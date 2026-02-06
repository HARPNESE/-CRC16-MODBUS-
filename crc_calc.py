import sys
import re
import json
import os
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QLineEdit, QPushButton, QSpacerItem,
    QSizePolicy, QFrame, QScrollArea, QFileDialog, QMessageBox
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont

# ===================== 核心工具&CRC计算函数 =====================
def crc16_modbus(data, order='little'):
    """标准CRC16-MODBUS计算（MODBUS专用，单字节0-255输入）"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte & 0xFF  # 强制单字节，防止溢出
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    crc &= 0xFFFF  # 确保16位结果
    if order == 'little':
        return [(crc & 0x00FF), (crc >> 8)]  # 低位在前（MODBUS默认）
    else:
        return [(crc >> 8), (crc & 0x00FF)]  # 高位在前

def is_two_hex_char(text):
    """判断是否为单独的两位16进制数（0A/FF/12，无前缀）"""
    text = text.strip().upper()
    hex_pattern = re.compile(r'^[0-9A-F]{2}$')
    return bool(hex_pattern.match(text))

def get_var_value(var_name, var_value_dict):
    """获取变量值（容错，无则返回0，强制单字节）"""
    return var_value_dict.get(var_name.strip().upper(), 0) & 0xFF

# ========== 核心统一：变量名生成函数（A/B+两位行号+两位列号） ==========
def generate_var_name(prefix, row_num, col):
    """
    严格生成格式：A/B + 两位行号 + 两位列号
    示例：row1-col1 → A0101；row1-col6 → B0106；row2-col10 → B0210
    """
    return f"{prefix}{row_num:02d}{col:02d}"

# ========== #/$高低位拆分核心解析函数（适配新变量名规则） ==========
def parse_high_low_hex(match, var_value_dict):
    """解析 #(公式) 取高8位 / $(公式) 取低8位，返回十进制字符串"""
    try:
        symbol = match.group(1)  # 匹配#或$
        expr = match.group(2)    # 匹配括号内的表达式
        # 替换表达式中的变量（严格匹配A/B后跟4位数字：A0101/B0106）
        def var_replace(m):
            var = m.group(1)
            return str(get_var_value(var, var_value_dict))
        # 正则修改为：匹配A/B + 4位数字（两位行+两位列）
        expr = re.sub(r'(?<!0X)([AB]\d{4})', var_replace, expr.upper())
        # 安全计算公式结果（支持0X16进制、十进制、基础运算）
        allowed = {'__builtins__': None, 'abs': abs, 'round': round}
        result = eval(expr, allowed)
        result_int = int(round(result))
        # 限制为16位数值，补零到4位16进制（确保能拆分为高低8位）
        hex_4_str = f"{result_int & 0xFFFF:04X}"
        # #取前两位（高8位），$取后两位（低8位），转十进制返回
        if symbol == '#':
            return str(int(hex_4_str[:2], 16))
        else:
            return str(int(hex_4_str[2:], 16))
    except Exception:
        return "0"  # 异常则返回0

def parse_b_formula(formula_text, var_value_dict):
    """
    解析B列公式（#/$处理 + 变量匹配A0101/B0106格式）
    """
    try:
        # 第一步：优先处理 #(公式) 和 $(公式) 高低位拆分
        formula_text = re.sub(
            r'([#$])\(([^)]+)\)',
            lambda m: parse_high_low_hex(m, var_value_dict),
            formula_text
        )
        # 第二步：替换公式中的变量（严格匹配A/B后跟4位数字）
        def var_replace(match):
            var = match.group(1)
            return str(get_var_value(var, var_value_dict))
        formula_text = re.sub(r'(?<!0X)([AB]\d{4})', var_replace, formula_text.upper())
        
        # 第三步：安全计算（仅允许基础运算，Python原生支持0X开头16进制）
        allowed_builtins = {'__builtins__': None}
        allowed_funcs = {'abs': abs, 'round': round}
        result = eval(formula_text, allowed_builtins, allowed_funcs)
        
        # 强制单字节（0-255），符合MODBUS字节要求
        return int(round(result)) & 0xFF if isinstance(result, float) else result & 0xFF
    except Exception:
        return 0

def parse_b_input(input_text, var_value_dict):
    """
    核心解析B列输入（含#/$也判定为公式 + 匹配A0101/B0106变量）
    """
    input_text = input_text.strip()
    if not input_text:
        return 0

    # 情况1：包含运算符 或 含#/$ → 公式计算
    if any(op in input_text for op in '+-*/#$'):
        return parse_b_formula(input_text, var_value_dict)
    
    # 情况2：两位字符 → 纯16进制数（0A/FF/12）
    if len(input_text) == 2:
        try:
            return int(input_text, 16) & 0xFF
        except ValueError:
            return 0
    
    # 情况3：4位及以上 → 变量引用（A0101/B0106）
    return get_var_value(input_text, var_value_dict)

# ===================== 主窗口类（严格保留原布局，仅修复变量名） =====================
class CRC16MODBUSCalculator(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("中控代码计算器V1.0")
        self.setGeometry(100, 100, 1400, 800)
        self.setMinimumSize(1000, 600)

        # 全局可配置参数（1-30行、0-4个A列、1-20个B列）
        self.total_rows = 1          # 计算行数
        self.dec_col_count = 0       # 每行A列十进制项数
        self.hex_col_count = 6       # 每行B列16进制/变量/公式项数
        self.crc_order = "低位在前"   # CRC16结果字节序

        # 核心数据字典（全程保留原始输入，不修改）
        self.raw_text_dict = {}      # {变量名: 原始输入文本} 如{"A0101":"20", "B0106":"0A"}
        self.var_value_dict = {}     # {变量名: 解析后十进制值} 如{"A0101":20, "B0106":10}
        self.row_widgets = {}        # 存储所有行控件引用，方便刷新/计算

        # 防抖定时器（200ms，避免输入时频繁计算，提升界面流畅度）
        self.calc_timer = QTimer()
        self.calc_timer.setInterval(200)
        self.calc_timer.timeout.connect(self.calc_all_rows)

        # 初始化顶部菜单栏
        self.init_menu_bar()
        # 初始化主界面
        self.init_main_ui()

    # 初始化顶部菜单栏：文件（导入/导出）、关于
    def init_menu_bar(self):
        # 创建主菜单栏
        menu_bar = self.menuBar()
        menu_bar.setFont(QFont("SimHei", 10))

        # 1. 文件菜单：包含导入配置、导出配置
        file_menu = menu_bar.addMenu("文件(&F)")
        # 导出配置动作
        export_act = file_menu.addAction("导出配置(&E)")
        export_act.triggered.connect(self.export_config)
        # 导入配置动作
        import_act = file_menu.addAction("导入配置(&I)")
        import_act.triggered.connect(self.import_config)

        # 2. 关于菜单：展示作者和版本
        about_menu = menu_bar.addMenu("关于(&A)")
        about_act = about_menu.addAction("关于软件(&S)")
        about_act.triggered.connect(self.show_about)

    # 导出配置：将全局参数+所有原始输入文本保存为JSON文件（自动补后缀）
    def export_config(self):
        # 选择保存文件路径，过滤为JSON文件
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "导出配置", "", "JSON配置文件 (*.json);;所有文件 (*.*)",
            options=QFileDialog.DontUseNativeDialog
        )
        if not file_path:
            return  # 用户取消选择
        
        # 自动添加.json后缀（用户未输入时）
        if not os.path.splitext(file_path)[1]:  # 无后缀时
            if selected_filter == "JSON配置文件 (*.json)":
                file_path += ".json"  # 补JSON后缀
        
        # 构造要导出的配置数据
        config_data = {
            "global_params": {
                "total_rows": self.total_rows,
                "dec_col_count": self.dec_col_count,
                "hex_col_count": self.hex_col_count,
                "crc_order": self.crc_order
            },
            "raw_text_dict": self.raw_text_dict  # 所有输入框的原始文本
        }

        # 写入JSON文件（格式化输出，易读）
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, ensure_ascii=False, indent=4)
            QMessageBox.information(self, "导出成功", f"配置已成功导出到：\n{file_path}", QMessageBox.Ok)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"配置导出出错：\n{str(e)}", QMessageBox.Ok)

    # 导入配置：读取JSON文件，恢复全局参数+回填所有输入文本
    def import_config(self):
        # 选择要导入的JSON文件
        file_path, _ = QFileDialog.getOpenFileName(
            self, "导入配置", "", "JSON配置文件 (*.json);;所有文件 (*.*)",
            options=QFileDialog.DontUseNativeDialog
        )
        if not file_path:
            return  # 用户取消选择
        
        # 读取并解析JSON文件
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "文件读取失败", f"无法读取配置文件：\n{str(e)}", QMessageBox.Ok)
            return

        # 校验配置文件格式（必须包含global_params和raw_text_dict）
        if "global_params" not in config_data or "raw_text_dict" not in config_data:
            QMessageBox.warning(self, "格式错误", "配置文件格式不合法，缺少核心参数！", QMessageBox.Ok)
            return

        # 提取全局参数并校验范围（防止非法值）
        global_params = config_data["global_params"]
        try:
            total_rows = max(1, min(30, global_params.get("total_rows", 1)))  # 1-30行
            dec_col_count = max(0, min(4, global_params.get("dec_col_count", 0)))  # 0-4列
            hex_col_count = max(1, min(20, global_params.get("hex_col_count", 6)))  # 1-20列
            crc_order = global_params.get("crc_order", "低位在前")
            if crc_order not in ["低位在前", "高位在前"]:
                crc_order = "低位在前"
        except Exception:
            QMessageBox.warning(self, "参数错误", "配置文件中全局参数不合法，使用默认值！", QMessageBox.Ok)
            return

        # 恢复核心数据：原始文本字典
        self.raw_text_dict = config_data.get("raw_text_dict", {})
        # 清空变量数值字典（后续会自动重新计算）
        self.var_value_dict.clear()

        # 分步恢复全局配置（触发原有布局刷新逻辑）
        self.row_combo.setCurrentText(str(total_rows))  # 恢复行数
        self.dec_combo.setCurrentText(str(dec_col_count))  # 恢复A列数
        self.hex_combo.setCurrentText(str(hex_col_count))  # 恢复B列数
        self.crc_combo.setCurrentText(crc_order)  # 恢复CRC顺序

        # 回填所有输入框的原始文本（遍历所有行的A/B输入框）
        for row_num in self.row_widgets:
            row_data = self.row_widgets[row_num]
            # 回填A列输入框
            for var_name, edit in row_data["dec_inputs"].items():
                edit.setText(self.raw_text_dict.get(var_name, ""))
            # 回填B列输入框
            for var_name, edit in row_data["hex_inputs"].items():
                edit.setText(self.raw_text_dict.get(var_name, ""))

        # 强制重新计算所有行
        self.calc_all_rows()
        QMessageBox.information(self, "导入成功", "配置已成功导入并生效！", QMessageBox.Ok)

    # 展示关于窗口：作者Harpnese，版本V1.0
    def show_about(self):
        about_text = (
            "中控代码计算器 V1.0\n"
            "=========================\n"
            "作者：Harpnese\n"
            "变量规则：A/B + 两位行号 + 两位列号（如A0101、B0106）\n"
            "功能：支持多行MODBUS CRC16校验计算，支持#/$函数进行高低位拆分、公式计算、跨行变量引用\n"
        )
        QMessageBox.about(self, "关于软件", about_text)

    def init_main_ui(self):
        """初始化界面：严格保留原布局，无任何调整"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(40, 40, 40, 40)

        # -------- 顶部全局配置栏（完全保留原样式） --------
        config_frame = QFrame()
        config_frame.setStyleSheet("QFrame{border:1px solid #ccc; border-radius:6px; padding:15px; background:#f8f8f8;}")
        config_layout = QHBoxLayout(config_frame)
        config_layout.setSpacing(30)
        config_layout.setAlignment(Qt.AlignLeft)

        # 创建配置项：行数、A列数、B列数、CRC顺序（参数完全保留）
        self.row_combo = self._create_config_item(config_layout, "计算项行数：", [str(i) for i in range(1,31)], "1", self.on_row_count_change)
        self.dec_combo = self._create_config_item(config_layout, "A行输入框项数：", [str(i) for i in range(0,5)], "0", self.on_dec_col_change)
        self.hex_combo = self._create_config_item(config_layout, "B行输入框项数：", [str(i) for i in range(1,21)], "6", self.on_hex_col_change)
        self.crc_combo = self._create_config_item(config_layout, "CRC16结果顺序：", ["低位在前", "高位在前"], "低位在前", self.on_crc_order_change)

        config_layout.addSpacerItem(QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        main_layout.addWidget(config_frame)

        # -------- 核心输入规则提示（更新变量名规则说明） --------
        rule_label = QLabel(
            "📌 输入规则：\n"
            "1. A行=纯十进制（20/4000）；B行=16进制/变量/公式\n"
            "2. B行单独输入→两位=16进制（0A/FF）、四位=变量（A0101/B0106）\n"
            "3. B行公式计算→含+*-/，公式内16进制强制0X前缀（0X0A/0XFF）\n"
            "4. 高低位拆分函数→#(公式)取4位16进制前两位（高8位）、$(公式)取后两位（低8位）\n"
            "5. 公式示例：#(45002+A0101*8-4001)、$(B0101*3+0X20)、B0106+0XFF*2\n"
            "6. 变量名规则：A/B+两位行号+两位列号（如第1行第6列=B0106）"
        )
        rule_label.setFont(QFont("SimHei", 10, QFont.Bold))
        rule_label.setStyleSheet("color:#d9534f; padding:10px; border:2px solid #d9534f; border-radius:4px; background:#fff5f5;")
        rule_label.setWordWrap(True)
        main_layout.addWidget(rule_label)

        # -------- 滚动计算区域（完全保留原样式） --------
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea{border:1px solid #ccc; border-radius:6px;}")
        scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(scroll_widget)
        self.scroll_layout.setSpacing(15)
        self.scroll_layout.setAlignment(Qt.AlignTop)
        scroll_area.setWidget(scroll_widget)
        main_layout.addWidget(scroll_area, stretch=1)

        # 初始化第一行计算项
        self.add_calc_row(1)

    def _create_config_item(self, layout, label_text, options, default, callback):
        """快速创建配置项：完全保留原样式"""
        label = QLabel(label_text)
        label.setMinimumWidth(150)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        label.setFont(QFont("SimHei", 10))
        layout.addWidget(label)

        combo = QComboBox()
        combo.addItems(options)
        combo.setCurrentText(default)
        combo.setFixedWidth(100)
        combo.setFont(QFont("SimHei", 10))
        combo.currentTextChanged.connect(callback)
        layout.addWidget(combo)
        return combo

    # ===================== 行操作：严格保留原布局，仅替换变量名生成逻辑 =====================
    def add_calc_row(self, row_num):
        """新增一行计算项：完全保留原布局（输入框宽度、间距、样式均不变）"""
        margin_X=10
        row_frame = QFrame()
        row_frame.setStyleSheet("QFrame{border:1px solid #ccc; border-radius:6px; padding:15px; margin:5px 0;}")
        row_layout = QVBoxLayout(row_frame)
        row_layout.setSpacing(margin_X)

        # 行标题（完全保留）
        row_title = QLabel(f"第{row_num}行 | A行输入（十进制）、B行输入（16进制/变量/公式）")
        row_title.setFont(QFont("SimHei", 11, QFont.Bold))
        row_layout.addWidget(row_title)

        # -------- 1. A列：十进制输入行（完全保留原布局） --------
        dec_layout = QHBoxLayout()
        dec_layout.setSpacing(margin_X)
        dec_label = QLabel("A行")
        dec_label.setFixedWidth(80)
        dec_label.setAlignment(Qt.AlignCenter)
        dec_layout.addWidget(dec_label)

        dec_container = QWidget()
        dec_container_layout = QHBoxLayout(dec_container)
        dec_container_layout.setSpacing(margin_X)
        dec_layout.addWidget(dec_container)
        dec_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        row_layout.addLayout(dec_layout)

        # -------- 2. B列：16进制/变量/公式输入行 + CRC16显示（完全保留原布局） --------
        hex_crc_layout = QHBoxLayout()
        hex_crc_layout.setSpacing(margin_X)
        hex_label = QLabel("B行")
        hex_label.setFixedWidth(80)
        hex_label.setAlignment(Qt.AlignCenter)
        hex_crc_layout.addWidget(hex_label)

        hex_container = QWidget()
        hex_container_layout = QHBoxLayout(hex_container)
        hex_container_layout.setSpacing(margin_X)
        hex_crc_layout.addWidget(hex_container)

        # CRC16显示区（完全保留原样式：宽度120、高度40、红字）
        crc_v_layout = QVBoxLayout()
        crc_label = QLabel(f"CRC16（{self.crc_order}）")
        crc_label.setAlignment(Qt.AlignCenter)
        crc_label.setFont(QFont("SimHei", 10))
        crc_input = QLineEdit()
        crc_input.setReadOnly(True)
        crc_input.setStyleSheet("QLineEdit{background:#f5f5f5; color:#e63946; font-weight:bold; font-size:14px;}")
        crc_input.setFixedWidth(120)
        crc_input.setFixedHeight(40)
        crc_input.setFont(QFont("Consolas", 11))
        crc_v_layout.addWidget(crc_label)
        crc_v_layout.addWidget(crc_input)
        hex_crc_layout.addLayout(crc_v_layout)
        hex_crc_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        row_layout.addLayout(hex_crc_layout)

        # -------- 3. 最终结果行 + 一键复制按钮（完全保留原布局） --------
        result_layout = QHBoxLayout()
        result_layout.setSpacing(margin_X)
        result_label = QLabel("最终结果：")
        result_label.setFixedWidth(100)
        result_label.setAlignment(Qt.AlignCenter)
        result_layout.addWidget(result_label)

        result_input = QLineEdit()
        result_input.setReadOnly(True)
        result_input.setFont(QFont("Consolas", 11))
        result_input.setMinimumWidth(700)
        result_layout.addWidget(result_input)

        # 复制按钮（完全保留原样式：绿色、宽度100、高度40）
        copy_btn = QPushButton("复制结果")
        copy_btn.setFixedWidth(100)
        copy_btn.setFixedHeight(40)
        copy_btn.setFont(QFont("SimHei", 10))
        copy_btn.setStyleSheet("QPushButton{background:#5cb85c; color:white; border:none; border-radius:4px;} QPushButton:hover{background:#4cae4c;}")
        copy_btn.clicked.connect(lambda: self.copy_row_result(row_num))
        result_layout.addWidget(copy_btn)
        result_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        row_layout.addLayout(result_layout)

        # 添加到滚动布局
        self.scroll_layout.addWidget(row_frame)

        # 存储该行所有控件引用（完全保留）
        self.row_widgets[row_num] = {
            "frame": row_frame,
            "dec_container": dec_container_layout,
            "dec_inputs": {},  # {A0101: QLineEdit, A0102: QLineEdit}
            "hex_container": hex_container_layout,
            "hex_inputs": {},  # {B0101: QLineEdit, B0106: QLineEdit}
            "crc_input": crc_input,
            "result_input": result_input,
            "copy_btn": copy_btn
        }

        # 刷新该行的A/B列输入框
        self.refresh_dec_inputs(row_num)
        self.refresh_hex_inputs(row_num)

    def remove_calc_row(self, row_num):
        """删除指定行：适配新变量名规则"""
        if row_num in self.row_widgets:
            # 清理原始文本和变量数值字典（匹配A01xx/B01xx格式）
            del_prefix_a = f"A{row_num:02d}"  # 匹配A01xx
            del_prefix_b = f"B{row_num:02d}"  # 匹配B01xx
            del_var_list = [k for k in self.raw_text_dict if k.startswith(del_prefix_a) or k.startswith(del_prefix_b)]
            for var_name in del_var_list:
                self.raw_text_dict.pop(var_name, None)
                self.var_value_dict.pop(var_name, None)
            # 清理界面控件
            self.row_widgets[row_num]["frame"].deleteLater()
            del self.row_widgets[row_num]

    def refresh_dec_inputs(self, row_num):
        """刷新A列输入框：保留原布局（宽度100），生成A0101格式变量名"""
        row_data = self.row_widgets[row_num]
        layout = row_data["dec_container"]
        inputs = row_data["dec_inputs"]

        # 清空原有输入框
        self._clear_layout(layout)
        inputs.clear()

        # 按配置创建A列输入框
        for col in range(1, self.dec_col_count + 1):
            # 生成A+两位行+两位列（如A0101）
            var_name = generate_var_name("A", row_num, col)
            # 变量名标签（完全保留原样式：宽度100、Consolas字体）
            var_label = QLabel(var_name)
            var_label.setFixedWidth(100)
            var_label.setAlignment(Qt.AlignCenter)
            var_label.setFont(QFont("Consolas", 10, QFont.Bold))
            # 十进制输入框（完全保留原样式：宽度100）
            edit = QLineEdit()
            edit.setPlaceholderText("输入十进制")
            edit.setFixedWidth(100)
            edit.setFont(QFont("Consolas", 10))
            # 绑定文本变化事件
            edit.textChanged.connect(lambda text, v=var_name: self.update_raw_text(v, text))
            # 初始化值
            if var_name in self.raw_text_dict:
                edit.setText(self.raw_text_dict[var_name])
            # 标签+输入框垂直布局（完全保留）
            v_layout = QVBoxLayout()
            v_layout.addWidget(var_label)
            v_layout.addWidget(edit)
            layout.addLayout(v_layout)
            inputs[var_name] = edit

    def refresh_hex_inputs(self, row_num):
        """刷新B列输入框：保留原布局（宽度100），生成B0106格式变量名"""
        row_data = self.row_widgets[row_num]
        layout = row_data["hex_container"]
        inputs = row_data["hex_inputs"]

        # 清空原有输入框
        self._clear_layout(layout)
        inputs.clear()

        # 按配置创建B列输入框
        for col in range(1, self.hex_col_count + 1):
            # 生成B+两位行+两位列（如B0106）
            var_name = generate_var_name("B", row_num, col)
            # 变量名标签（完全保留原样式：宽度100、Consolas字体）
            var_label = QLabel(var_name)
            var_label.setFixedWidth(100)
            var_label.setAlignment(Qt.AlignCenter)
            var_label.setFont(QFont("Consolas", 10, QFont.Bold))
            # B列输入框（完全保留原样式：宽度100）
            edit = QLineEdit()
            edit.setPlaceholderText("")
            edit.setFixedWidth(100)
            edit.setFont(QFont("Consolas", 10))
            # 绑定文本变化事件
            edit.textChanged.connect(lambda text, v=var_name: self.update_raw_text(v, text))
            # 初始化值
            if var_name in self.raw_text_dict:
                edit.setText(self.raw_text_dict[var_name])
            # 标签+输入框垂直布局（完全保留）
            v_layout = QVBoxLayout()
            v_layout.addWidget(var_label)
            v_layout.addWidget(edit)
            layout.addLayout(v_layout)
            inputs[var_name] = edit

    def _clear_layout(self, layout):
        """递归清空布局：完全保留原逻辑"""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    # ===================== 全局配置变化响应（完全保留原逻辑） =====================
    def on_row_count_change(self, value):
        """计算行数变化响应：完全保留"""
        new_count = int(value)
        old_count = self.total_rows
        # 新增行
        for row_num in range(old_count + 1, new_count + 1):
            self.add_calc_row(row_num)
        # 删除行
        for row_num in range(old_count, new_count, -1):
            self.remove_calc_row(row_num)
        self.total_rows = new_count
        self.calc_all_rows()

    def on_dec_col_change(self, value):
        """A列数变化响应：完全保留"""
        self.dec_col_count = int(value)
        for row_num in self.row_widgets:
            self.refresh_dec_inputs(row_num)
        self.calc_all_rows()

    def on_hex_col_change(self, value):
        """B列数变化响应：完全保留"""
        self.hex_col_count = int(value)
        for row_num in self.row_widgets:
            self.refresh_hex_inputs(row_num)
        self.calc_all_rows()

    def on_crc_order_change(self, value):
        """CRC顺序变化响应：完全保留"""
        self.crc_order = value
        for row_num in self.row_widgets:
            # 更新CRC标签
            crc_label = self.row_widgets[row_num]["crc_input"].parent().findChild(QLabel)
            crc_label.setText(f"CRC16（{self.crc_order}）")
        self.calc_all_rows()

    # ===================== 核心业务逻辑：适配新变量名规则 =====================
    def update_raw_text(self, var_name, text):
        """更新原始文本：完全保留"""
        self.raw_text_dict[var_name] = text.strip()
        if not self.calc_timer.isActive():
            self.calc_timer.start()

    def calc_all_rows(self):
        """计算所有行：完全保留"""
        self.calc_timer.stop()
        self.update_all_var_values()
        for row_num in self.row_widgets:
            self.calc_single_row(row_num)

    def update_all_var_values(self):
        """更新变量值：使用A0101/B0106格式变量名"""
        # 第一步：计算A列
        for row_num in self.row_widgets:
            for col in range(1, self.dec_col_count + 1):
                var_name = generate_var_name("A", row_num, col)
                raw_text = self.raw_text_dict.get(var_name, "")
                self.var_value_dict[var_name] = int(raw_text) if raw_text.isdigit() else 0

        # 第二步：计算B列（嵌套引用）
        has_value_change = True
        max_attempts = 5
        current_attempt = 0

        while has_value_change and current_attempt < max_attempts:
            has_value_change = False
            current_attempt += 1

            for row_num in self.row_widgets:
                for col in range(1, self.hex_col_count + 1):
                    var_name = generate_var_name("B", row_num, col)
                    raw_text = self.raw_text_dict.get(var_name, "")
                    old_value = self.var_value_dict.get(var_name, 0)
                    new_value = parse_b_input(raw_text, self.var_value_dict)
                    if new_value != old_value:
                        self.var_value_dict[var_name] = new_value
                        has_value_change = True

    def calc_single_row(self, row_num):
        """计算单行结果：使用A0101/B0106格式变量名"""
        row_data = self.row_widgets[row_num]
        b_col_dec_values = []

        # 1. 收集B列数值
        for col in range(1, self.hex_col_count + 1):
            var_name = generate_var_name("B", row_num, col)
            b_val = self.var_value_dict.get(var_name, 0) & 0xFF
            b_col_dec_values.append(b_val)

        # 2. 计算CRC16
        crc_order = 'little' if self.crc_order == "低位在前" else 'big'
        crc_byte1, crc_byte2 = crc16_modbus(b_col_dec_values, crc_order)
        crc_hex_str = f"{crc_byte1:02X}{crc_byte2:02X}"

        # 3. 拼接最终结果
        b_col_hex_list = [f"{val:02X}" for val in b_col_dec_values]
        final_hex_list = b_col_hex_list + [f"{crc_byte1:02X}", f"{crc_byte2:02X}"]
        final_hex_str = " ".join(final_hex_list)

        # 4. 更新显示
        row_data["crc_input"].setText(crc_hex_str)
        row_data["result_input"].setText(final_hex_str)

    # ===================== 辅助功能：一键复制（完全保留原逻辑） =====================
    def copy_row_result(self, row_num):
        """复制结果：完全保留"""
        row_data = self.row_widgets[row_num]
        pure_hex_result = row_data["result_input"].text().replace(" ", "")
        QApplication.clipboard().setText(pure_hex_result)
        original_btn_text = row_data["copy_btn"].text()
        row_data["copy_btn"].setText("已复制✅")
        QTimer.singleShot(1000, lambda: row_data["copy_btn"].setText(original_btn_text))

# ===================== 程序入口 =====================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("SimHei", 10))  # 全局中文字体
    main_window = CRC16MODBUSCalculator()
    main_window.show()
    sys.exit(app.exec_())